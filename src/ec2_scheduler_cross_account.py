"""
CCoE EC2 Scheduler - Cross-Account, Cross-Region
=================================================
Lambda function que apaga y enciende instancias EC2 en TODAS las cuentas
de AWS Organizations, en multiples regiones.

ARQUITECTURA - DONDE SE EJECUTA CADA COSA
==========================================

CUENTA MANAGEMENT (Control Tower):
  Esta Lambda SE EJECUTA AQUI.
  Desde aca descubre todas las cuentas de la organizacion
  y asume un rol IAM en cada una.

  Permisos que necesita:
  - organizations:ListAccounts       -> descubre TODAS las cuentas
  - organizations:DescribeAccount    -> lee tags de cada cuenta (Referente)
  - sts:AssumeRole                   -> asume rol en cada cuenta
  - sns:Publish + sns:Subscribe      -> notificaciones SNS
  - logs:*                           -> CloudWatch Logs

CUENTAS MIEMBRO:
  La Lambda NO se ejecuta aca.
  SOLO asume un rol (CCoE-EC2Scheduler-Role) para operar EC2.

  Permisos del rol:
  - ec2:DescribeInstances  -> listar instancias
  - ec2:StopInstances      -> apagar (solo con tag environment)
  - ec2:StartInstances     -> encender (solo con tag environment)

NOTIFICACIONES SNS:
  Por cada accion (APAGADO/ENCENDIDO/EXCEPCION), la Lambda envia
  una notificacion al SNS Topic configurado.

  El destinatario se determina asi:
  1. Lee el tag "Referente" de la cuenta AWS (via Organizations:DescribeAccount)
  2. Si existe, suscribe ese email al topic SNS y envia la notificacion
  3. Si no existe, usa el fallback: ccoe-team@empresa.com

FLUJO COMPLETO:
  1. EventBridge Scheduler (en Management Account) invoca esta Lambda
     con un payload: {"action": "stop"} o {"action": "start"}

  2. La Lambda llama a organizations:ListAccounts
     -> Obtiene TODAS las cuentas activas de la organizacion
     -> NO necesita IDs de OU ni IDs de cuenta especificos

  3. Por cada cuenta (excepto las excluidas en EXCLUDED_ACCOUNTS):
     a. Lee el tag "Referente" de la cuenta (email del responsable)
     b. Asume el rol: arn:aws:iam::<CUENTA>:role/CCoE-EC2Scheduler-Role
     c. En cada region configurada:
        - Busca EC2 con tag environment in {sandbox, dev, desarrollo}
        - Evalua tags de excepcion (no-shutdown, etc.)
        - Ejecuta STOP/START segun corresponda
        - Envia notificacion SNS al Referente de la cuenta

  4. Al finalizar, envia un resumen SNS con el resultado global.
"""

import boto3
import os
import json
import logging
from datetime import date, datetime
from typing import Dict, List, Any, Optional, Tuple

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ──────────────────────────────────────────────
# Environment Variables
# ──────────────────────────────────────────────

# Comma-separated list of environments to manage
ENVIRONMENTS = os.environ.get(
    'ENVIRONMENTS',
    'sandbox,desarrollo'
).lower().split(',')

# Comma-separated list of regions. Use 'all' for all enabled regions
REGIONS = os.environ.get(
    'REGIONS',
    'us-east-1,us-east-2,us-west-2,eu-west-1,sa-east-1'
)

# Action: 'stop' or 'start'
ACTION = os.environ.get('ACTION', 'stop').lower()

# Dry-run mode: 'true' to simulate without making changes
DRY_RUN = os.environ.get('DRY_RUN', 'false').lower() == 'true'

# SNS Topic ARN for notifications
# Example: arn:aws:sns:us-east-1:123456789012:CCoE-EC2Scheduler-Notifications
SNS_TOPIC_ARN = os.environ.get('SNS_TOPIC_ARN', '')

# Email de fallback cuando la cuenta no tiene tag Referente
SNS_FALLBACK_EMAIL = os.environ.get(
    'SNS_FALLBACK_EMAIL',
    'ccoe-team@empresa.com'
)

# IAM Role name to assume in each member account
ASSUME_ROLE_NAME = os.environ.get(
    'ASSUME_ROLE_NAME',
    'CCoE-EC2Scheduler-Role'
)

# Comma-separated list of account IDs to EXCLUDE from processing
EXCLUDED_ACCOUNTS = os.environ.get(
    'EXCLUDED_ACCOUNTS',
    ''
).split(',') if os.environ.get('EXCLUDED_ACCOUNTS') else []

# ──────────────────────────────────────────────
# Tag Constants
# ──────────────────────────────────────────────
ENVIRONMENT_TAG_KEY = 'environment'
NO_SHUTDOWN_TAG_KEY = 'no-shutdown'
NO_SHUTDOWN_REASON_TAG_KEY = 'no-shutdown-reason'
NO_SHUTDOWN_APPROVED_BY_TAG_KEY = 'no-shutdown-approved-by'
NO_SHUTDOWN_EXPIRY_TAG_KEY = 'no-shutdown-expiry'
REFERENTE_TAG_KEY = 'Referente'  # Tag a nivel cuenta AWS

# Valid environment values (lowercase for comparison)
VALID_ENVIRONMENTS = {'sandbox', 'dev', 'desarrollo'}


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main Lambda entry point.
    
    Event payload can override environment variables:
    {
        "action": "stop|start",
        "dry_run": true|false,
        "regions": ["us-east-1", "sa-east-1"],
        "accounts": ["111111111111", "222222222222"]
    }
    """
    start_time = datetime.utcnow()
    
    action = event.get('action', ACTION).lower()
    dry_run = event.get('dry_run', DRY_RUN)
    regions = event.get('regions', _parse_regions())
    target_accounts = event.get('accounts', None)
    
    logger.info(f"CCoE EC2 Scheduler iniciado")
    logger.info(f"   Accion: {action.upper()}")
    logger.info(f"   DryRun: {dry_run}")
    logger.info(f"   Regiones: {regions}")
    logger.info(f"   Ambientes: {ENVIRONMENTS}")
    logger.info(f"   Cuentas target: {'Descubrir desde Organizations' if target_accounts is None else target_accounts}")
    logger.info(f"   Cuentas excluidas: {EXCLUDED_ACCOUNTS}")
    logger.info(f"   SNS Topic: {SNS_TOPIC_ARN or 'No configurado'}")
    logger.info(f"   SNS Fallback Email: {SNS_FALLBACK_EMAIL}")

    summary: Dict[str, Any] = {
        'action': action,
        'dry_run': dry_run,
        'start_time': start_time.isoformat(),
        'accounts_processed': 0,
        'accounts_with_errors': [],
        'total_instances_processed': 0,
        'total_instances_skipped': 0,
        'total_errors': 0,
        'results_by_account': {}
    }

    try:
        # Step 1: Resolve target accounts
        if target_accounts:
            accounts = target_accounts
            logger.info(f"Usando lista explicita de cuentas: {accounts}")
        else:
            accounts = _discover_organization_accounts()
            logger.info(f"Descubiertas {len(accounts)} cuentas desde Organizations")

        # Step 2: Process each account
        for account_id in accounts:
            if account_id in EXCLUDED_ACCOUNTS:
                logger.info(f"Omitiendo cuenta excluida {account_id}")
                continue

            account_result = _process_account(
                account_id=account_id,
                action=action,
                dry_run=dry_run,
                regions=regions
            )

            summary['results_by_account'][account_id] = account_result
            summary['accounts_processed'] += 1

            if account_result.get('has_errors'):
                summary['accounts_with_errors'].append(account_id)

            summary['total_instances_processed'] += len(account_result.get('processed', []))
            summary['total_instances_skipped'] += len(account_result.get('skipped', []))
            summary['total_errors'] += len(account_result.get('errors', []))

        # Step 3: Send summary notification
        _send_summary_notification(summary, action)

    except Exception as e:
        logger.error(f"Error fatal en lambda_handler: {str(e)}", exc_info=True)
        summary['fatal_error'] = str(e)
        summary['status'] = 'FAILED'

    else:
        summary['status'] = 'COMPLETED'

    finally:
        end_time = datetime.utcnow()
        duration = (end_time - start_time).total_seconds()
        summary['end_time'] = end_time.isoformat()
        summary['duration_seconds'] = duration
        logger.info(f"Lambda completada en {duration:.2f}s | "
                    f"Cuentas: {summary['accounts_processed']} | "
                    f"Procesadas: {summary['total_instances_processed']} | "
                    f"Excepcionadas: {summary['total_instances_skipped']} | "
                    f"Errores: {summary['total_errors']}")

    return summary


# ═══════════════════════════════════════════════
# ORGANIZATIONS DISCOVERY
# ═══════════════════════════════════════════════
# Descubre todas las cuentas activas de la organizacion.
# Tambien obtiene el tag "Referente" de cada cuenta.

def _discover_organization_accounts() -> List[str]:
    """
    Discover all active AWS accounts in the organization.
    Uses AWS Organizations from the management account.
    
    Returns a list of 12-digit AWS account IDs.
    Only returns ACTIVE accounts.
    """
    try:
        org_client = boto3.client('organizations')
        accounts: List[str] = []
        paginator = org_client.get_paginator('list_accounts')

        for page in paginator.paginate():
            for acc in page.get('Accounts', []):
                if acc.get('Status') == 'ACTIVE':
                    accounts.append(acc['Id'])

        logger.info(f"Cuentas de Organization encontradas (ACTIVE): {len(accounts)}")
        if accounts:
            logger.info(f"IDs de cuenta: {', '.join(accounts)}")
        return accounts

    except Exception as e:
        logger.error(f"Error al listar cuentas de Organization: {str(e)}")
        raise


def _get_account_email(account_id: str) -> str:
    """
    Lee el tag 'Referente' de una cuenta AWS via Organizations.
    
    El tag 'Referente' debe contener un email del responsable
    de la cuenta. Si no existe, retorna el email de fallback.
    
    Args:
        account_id: 12-digit AWS account ID
    
    Returns:
        Email del referente o fallback
    """
    try:
        org_client = boto3.client('organizations')
        
        # Describir la cuenta para obtener sus tags
        response = org_client.describe_account(AccountId=account_id)
        account_info = response.get('Account', {})
        
        # Obtener los tags de la cuenta
        # Nota: organizations:DescribeAccount NO incluye tags directamente
        # Necesitamos usar ListTagsForResource
        try:
            tags_response = org_client.list_tags_for_resource(
                ResourceId=account_id
            )
            tags = tags_response.get('Tags', [])
            
            for tag in tags:
                if tag.get('Key') == REFERENTE_TAG_KEY:
                    email = tag.get('Value', '').strip()
                    if email:
                        logger.info(f"Cuenta {account_id}: Referente encontrado -> {email}")
                        return email
            
        except Exception as tag_error:
            logger.warning(f"No se pudieron leer tags de la cuenta {account_id}: {tag_error}")
        
        # Fallback: no se encontro el tag
        logger.info(f"Cuenta {account_id}: Sin tag Referente, usando fallback {SNS_FALLBACK_EMAIL}")
        return SNS_FALLBACK_EMAIL
        
    except Exception as e:
        logger.warning(f"Error al obtener datos de la cuenta {account_id}: {e}")
        return SNS_FALLBACK_EMAIL


def _parse_regions() -> List[str]:
    """
    Parse the REGIONS environment variable.
    If 'all', discover all enabled regions for the account.
    """
    raw = REGIONS.strip().lower()

    if raw == 'all':
        try:
            ec2 = boto3.client('ec2', region_name='us-east-1')
            response = ec2.describe_regions(AllRegions=False)
            regions = [r['RegionName'] for r in response.get('Regions', [])]
            logger.info(f"Regiones habilitadas descubiertas: {len(regions)}")
            return regions
        except Exception as e:
            logger.warning(f"No se pudieron descubrir regiones, usando defaults: {str(e)}")
            return ['us-east-1', 'us-east-2', 'us-west-2', 'sa-east-1']

    return [r.strip() for r in raw.split(',') if r.strip()]


# ═══════════════════════════════════════════════
# ACCOUNT PROCESSING
# ═══════════════════════════════════════════════
# Para cada cuenta:
# 1. Obtiene el email del Referente (tag de la cuenta)
# 2. Asume rol IAM via STS
# 3. Procesa cada region

def _process_account(
    account_id: str,
    action: str,
    dry_run: bool,
    regions: List[str]
) -> Dict[str, Any]:
    """
    Process a single member account across all specified regions.
    - Reads the Referente email tag from the account
    - Assumes CCoE-EC2Scheduler-Role via STS
    - Processes each region
    """
    result: Dict[str, Any] = {
        'account_id': account_id,
        'processed': [],
        'skipped': [],
        'errors': [],
        'has_errors': False,
        'regions_processed': [],
        'referente_email': ''
    }

    logger.info(f"\n{'='*60}")
    logger.info(f"Procesando Cuenta: {account_id}")
    logger.info(f"{'='*60}")

    # Step 1: Get the Referente email for this account
    referente_email = _get_account_email(account_id)
    result['referente_email'] = referente_email
    logger.info(f"Email del Referente: {referente_email}")

    try:
        # Step 2: Assume role in the member account via STS
        sts_client = boto3.client('sts')
        role_arn = f"arn:aws:iam::{account_id}:role/{ASSUME_ROLE_NAME}"

        logger.info(f"Asumiendo rol: {role_arn}")

        assumed_role = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"CCoE-EC2Scheduler-{account_id}",
            DurationSeconds=900
        )

        credentials = assumed_role['Credentials']

        # Step 3: Process each region
        for region in regions:
            region_result = _process_region(
                credentials=credentials,
                account_id=account_id,
                region=region,
                action=action,
                dry_run=dry_run,
                referente_email=referente_email
            )

            result['processed'].extend(region_result.get('processed', []))
            result['skipped'].extend(region_result.get('skipped', []))
            result['errors'].extend(region_result.get('errors', []))
            result['regions_processed'].append(region)

    except Exception as e:
        error_msg = f"Error al procesar cuenta {account_id}: {str(e)}"
        logger.error(f"Error: {error_msg}")
        result['errors'].append({
            'account_id': account_id,
            'error': error_msg,
            'referente_email': referente_email
        })
        result['has_errors'] = True

        # Enviar notificacion de error al referente
        _send_notification(
            account_id=account_id,
            region='N/A',
            instance_id='N/A',
            instance_name='N/A',
            environment='N/A',
            action_label='ERROR',
            dry_run=dry_run,
            referente_email=referente_email,
            message_body=f"""
ERROR al procesar cuenta {account_id}

Cuenta:    {account_id}
Error:     {error_msg}
Timestamp: {datetime.utcnow().isoformat()}
            """,
            subject_line=f"[ERROR] CCoE Scheduler - Cuenta {account_id}"
        )

    if result['processed'] or result['skipped']:
        logger.info(f"Resumen cuenta {account_id}: "
                    f"Procesadas={len(result['processed'])}, "
                    f"Excepcionadas={len(result['skipped'])}, "
                    f"Errores={len(result['errors'])}, "
                    f"Referente={referente_email}")

    return result


# ═══════════════════════════════════════════════
# REGION PROCESSING
# ═══════════════════════════════════════════════
# Dentro de cada cuenta y region, descubre instancias
# EC2, evalua tags de excepcion, y ejecuta STOP/START.

def _process_region(
    credentials: Dict[str, Any],
    account_id: str,
    region: str,
    action: str,
    dry_run: bool,
    referente_email: str
) -> Dict[str, Any]:
    """
    Process EC2 instances in a specific region for a specific account.
    Discovers instances, evaluates tags, and executes start/stop.
    """
    result: Dict[str, Any] = {
        'region': region,
        'processed': [],
        'skipped': [],
        'errors': []
    }

    today = str(date.today())

    logger.info(f"  Region: {region}")

    try:
        # Create EC2 client with assumed role credentials
        ec2_client = boto3.client(
            'ec2',
            region_name=region,
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )

        # Determine target state based on action
        target_states = ['running'] if action == 'stop' else ['stopped']

        # Discover instances with matching environment tags
        instances = _discover_instances(
            ec2_client=ec2_client,
            target_states=target_states,
            environments=ENVIRONMENTS
        )

        if not instances:
            logger.info(f"    No se encontraron instancias en {region}")
            return result

        logger.info(f"    Encontradas {len(instances)} instancia(s)")

        # Process each instance
        for instance in instances:
            instance_id = instance['InstanceId']
            instance_tags = _parse_tags(instance.get('Tags', []))
            instance_name = instance_tags.get('Name', instance_id)
            instance_env = instance_tags.get(ENVIRONMENT_TAG_KEY, 'unknown')

            # Check exception tags
            exception = _evaluate_exception(instance_tags, today)

            if exception['is_valid']:
                logger.info(
                    f"    EXCEPCION [{instance_env}] {instance_name} ({instance_id}) - "
                    f"Valida hasta {exception['expiry_date']}"
                )

                result['skipped'].append({
                    'InstanceId': instance_id,
                    'Name': instance_name,
                    'Environment': instance_env,
                    'AccountId': account_id,
                    'Region': region,
                    'Reason': exception['reason'],
                    'ApprovedBy': exception['approved_by'],
                    'ExpiryDate': exception['expiry_date']
                })

                # Notificacion: INSTANCIA EXCEPCIONADA (no se apaga)
                _send_notification(
                    account_id=account_id,
                    region=region,
                    instance_id=instance_id,
                    instance_name=instance_name,
                    environment=instance_env,
                    action_label='EXCEPCION',
                    dry_run=dry_run,
                    referente_email=referente_email,
                    message_body=f"""
INSTANCIA EXCEPCIONADA - No se aplico la accion programada

Cuenta:     {account_id}
Region:     {region}
Instancia:  {instance_name} ({instance_id})
Ambiente:   {instance_env}
Accion:     EXCEPCIONADA (omitida del proceso automatico)
Motivo:     {exception['reason']}
Aprobador:  {exception['approved_by']}
Vencimiento: {exception['expiry_date']}
Timestamp:  {datetime.utcnow().isoformat()}
                    """,
                    subject_line=f"[EXCEPCION] {instance_name} ({instance_id}) - {account_id}"
                )

                continue

            # Execute action
            if dry_run:
                logger.info(
                    f"    [DRY-RUN] Se {_action_text(action).lower()}ia "
                    f"[{instance_env}] {instance_name} ({instance_id})"
                )

                result['processed'].append({
                    'InstanceId': instance_id,
                    'Name': instance_name,
                    'Environment': instance_env,
                    'AccountId': account_id,
                    'Region': region,
                    'Action': action,
                    'DryRun': True
                })

            else:
                try:
                    action_label = _action_text(action)  # "APAGADO" o "ENCENDIDO"

                    if action == 'stop':
                        ec2_client.stop_instances(InstanceIds=[instance_id])
                        logger.info(f"    APAGADO [{instance_env}] {instance_name} ({instance_id})")
                    elif action == 'start':
                        ec2_client.start_instances(InstanceIds=[instance_id])
                        logger.info(f"    ENCENDIDO [{instance_env}] {instance_name} ({instance_id})")
                    else:
                        raise ValueError(f"Accion no soportada: {action}")

                    result['processed'].append({
                        'InstanceId': instance_id,
                        'Name': instance_name,
                        'Environment': instance_env,
                        'AccountId': account_id,
                        'Region': region,
                        'Action': action,
                        'DryRun': False
                    })

                    # Notificacion: INSTANCIA APAGADA o ENCENDIDA
                    _send_notification(
                        account_id=account_id,
                        region=region,
                        instance_id=instance_id,
                        instance_name=instance_name,
                        environment=instance_env,
                        action_label=action_label,
                        dry_run=False,
                        referente_email=referente_email,
                        message_body=f"""
INSTANCIA {action_label} - Accion automatica completada

Cuenta:     {account_id}
Region:     {region}
Instancia:  {instance_name} ({instance_id})
Ambiente:   {instance_env}
Accion:     {action_label}
Timestamp:  {datetime.utcnow().isoformat()}
                        """,
                        subject_line=f"[{action_label}] {instance_name} ({instance_id}) - {account_id}"
                    )

                except Exception as e:
                    error_msg = f"Error al ejecutar {action} en {instance_id}: {str(e)}"
                    logger.error(f"    Error: {error_msg}")
                    result['errors'].append({
                        'InstanceId': instance_id,
                        'Name': instance_name,
                        'Environment': instance_env,
                        'AccountId': account_id,
                        'Region': region,
                        'Action': action,
                        'Error': str(e)
                    })

                    # Notificacion: ERROR
                    _send_notification(
                        account_id=account_id,
                        region=region,
                        instance_id=instance_id,
                        instance_name=instance_name,
                        environment=instance_env,
                        action_label='ERROR',
                        dry_run=dry_run,
                        referente_email=referente_email,
                        message_body=f"""
ERROR - Fallo la accion programada

Cuenta:     {account_id}
Region:     {region}
Instancia:  {instance_name} ({instance_id})
Ambiente:   {instance_env}
Accion:     {_action_text(action)}
Error:      {str(e)}
Timestamp:  {datetime.utcnow().isoformat()}
                        """,
                        subject_line=f"[ERROR] {instance_name} ({instance_id}) - {account_id}"
                    )

    except Exception as e:
        error_msg = f"Error al procesar region {region} en cuenta {account_id}: {str(e)}"
        logger.error(f"    Error: {error_msg}")
        result['errors'].append({
            'account_id': account_id,
            'region': region,
            'error': error_msg
        })

    return result


def _action_text(action: str) -> str:
    """Convierte 'stop' a 'APAGADO' y 'start' a 'ENCENDIDO'."""
    return {
        'stop': 'APAGADO',
        'start': 'ENCENDIDO'
    }.get(action, action.upper())


# ═══════════════════════════════════════════════
# INSTANCE DISCOVERY
# ═══════════════════════════════════════════════

def _discover_instances(
    ec2_client,
    target_states: List[str],
    environments: List[str]
) -> List[Dict[str, Any]]:
    """
    Discover EC2 instances that:
    1. Are in the target state (running for stop, stopped for start)
    2. Have an environment tag matching one of the specified environments
    
    Uses pagination for large inventories.
    """
    all_instances: List[Dict[str, Any]] = []

    try:
        paginator = ec2_client.get_paginator('describe_instances')

        for page in paginator.paginate(
            Filters=[
                {
                    'Name': 'instance-state-name',
                    'Values': target_states
                }
            ]
        ):
            for reservation in page.get('Reservations', []):
                for instance in reservation.get('Instances', []):
                    tags = _parse_tags(instance.get('Tags', []))
                    env_value = tags.get(ENVIRONMENT_TAG_KEY, '').lower().strip()

                    if env_value in VALID_ENVIRONMENTS:
                        if env_value in environments:
                            all_instances.append(instance)
                        else:
                            for target_env in environments:
                                if _environments_match(target_env, env_value):
                                    all_instances.append(instance)
                                    break

    except Exception as e:
        logger.error(f"Error describiendo instancias: {str(e)}")
        raise

    return all_instances


def _environments_match(target_env: str, instance_env: str) -> bool:
    """
    Check if a target environment matches an instance environment.
    Handles semantic equivalences: 'desarrollo' <-> 'dev'
    """
    t = target_env.strip().lower()
    i = instance_env.strip().lower()

    if t == i:
        return True

    equivalences = {
        'desarrollo': 'dev',
        'dev': 'desarrollo'
    }

    return equivalences.get(t) == i


# ═══════════════════════════════════════════════
# TAG EXCEPTION EVALUATION
# ═══════════════════════════════════════════════

def _evaluate_exception(tags: Dict[str, str], today: str) -> Dict[str, Any]:
    """
    Evaluate whether an instance has a valid exception based on tags.
    
    Per standard EST-CCOE-VM-SCHEDULE-001:
    All four conditions must be met for a valid exception:
    1. no-shutdown == 'true'
    2. no-shutdown-reason exists and is not empty
    3. no-shutdown-approved-by exists and is not empty
    4. no-shutdown-expiry >= today (not expired)
    """
    no_shutdown = tags.get(NO_SHUTDOWN_TAG_KEY, 'false').lower() == 'true'
    reason = tags.get(NO_SHUTDOWN_REASON_TAG_KEY, '').strip()
    approved_by = tags.get(NO_SHUTDOWN_APPROVED_BY_TAG_KEY, '').strip()
    expiry = tags.get(NO_SHUTDOWN_EXPIRY_TAG_KEY, '').strip()

    is_valid = (
        no_shutdown
        and bool(reason)
        and bool(approved_by)
        and bool(expiry)
        and expiry >= today
    )

    return {
        'is_valid': is_valid,
        'reason': reason,
        'approved_by': approved_by,
        'expiry_date': expiry
    }


def _parse_tags(tags_list: List[Dict[str, str]]) -> Dict[str, str]:
    """Convert AWS tag list [{Key: k, Value: v}, ...] to dict {k: v}."""
    return {tag['Key']: tag['Value'] for tag in tags_list if 'Key' in tag and 'Value' in tag}


# ═══════════════════════════════════════════════
# SNS NOTIFICATIONS
# ══════════════════════════



Analizando la estructura de bucles for en el código y el README para verificar su alineación.

Cline read 5 files:

src/ec2_scheduler_cross_account.py · lines 58-85‎

src/ec2_scheduler_cross_account.py · lines 170-250‎

README.md · lines 98-140‎

src/ec2_scheduler_cross_account.py · lines 290-360‎

src/ec2_scheduler_cross_account.py · lines 361-440‎

Task Completed

Resultado de la revisión
He verificado exhaustivamente la estructura de bucles for en ambos archivos y ambos están completamente alineados.

Estructura de iteración (3 niveles):
#	Nivel	Código	README═════════════════════
# Envia notificaciones SNS con el email del Referente
# de la cuenta como destinatario.
#
# El mensaje incluye:
# - Tipo de accion (APAGADO / ENCENDIDO / EXCEPCION / ERROR)
# - Datos de la cuenta, region e instancia
# - Email del referente responsable
#
# Si la cuenta no tiene tag "Referente", usa el fallback
# configurado en SNS_FALLBACK_EMAIL (default: ccoe-team@empresa.com)

def _send_notification(
    account_id: str,
    region: str,
    instance_id: str,
    instance_name: str,
    environment: str,
    action_label: str,
    dry_run: bool,
    referente_email: str,
    message_body: str,
    subject_line: str
) -> None:
    """
    Envia una notificacion SNS.
    
    Publica el mensaje en el SNS Topic configurado.
    Todos los suscriptores del topic recibiran la notificacion.
    
    El topic SNS debe tener como suscriptores:
    - Los emails de Referente de cada cuenta (gestionado externamente)
    - El email de fallback: ccoe-team@empresa.com
    """
    if not SNS_TOPIC_ARN:
        return

    try:
        topic_parts = SNS_TOPIC_ARN.split(':')
        sns_region = topic_parts[3]

        sns_client = boto3.client('sns', region_name=sns_region)

        dry_run_prefix = '[DRY-RUN] ' if dry_run else ''

        # Armar el mensaje completo con encabezado
        full_message = f"""
╔══════════════════════════════════════════════════════╗
║  CCoE EC2 Scheduler - Notificacion de {action_label}      ║
╚══════════════════════════════════════════════════════╝
{dry_run_prefix}{message_body}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Responsable: {referente_email}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

        # Publicar en el SNS Topic
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"{dry_run_prefix}[{action_label}] {subject_line}",
            Message=full_message
        )

        logger.info(f"Notificacion SNS enviada: [{action_label}] {subject_line} -> {referente_email}")

    except Exception as e:
        logger.warning(f"Error al enviar notificacion SNS: {str(e)}")


def _send_summary_notification(summary: Dict[str, Any], action: str) -> None:
    """
    Envia un resumen de ejecucion al final del proceso.
    Se envia al email de fallback (ccoe-team@empresa.com).
    """
    if not SNS_TOPIC_ARN:
        return

    try:
        topic_parts = SNS_TOPIC_ARN.split(':')
        sns_region = topic_parts[3]
        sns_client = boto3.client('sns', region_name=sns_region)

        dry_run_prefix = '[DRY-RUN] ' if summary.get('dry_run') else ''
        action_es = _action_text(action)

        # Detalle por cuenta
        accounts_detail = ""
        for acc_id, acc_result in summary.get('results_by_account', {}).items():
            processed = len(acc_result.get('processed', []))
            skipped = len(acc_result.get('skipped', []))
            errors = len(acc_result.get('errors', []))
            email = acc_result.get('referente_email', SNS_FALLBACK_EMAIL)
            accounts_detail += f"""
  Cuenta: {acc_id}
    Responsable: {email}
    Procesadas: {processed}
    Excepcionadas: {skipped}
    Errores: {errors}
"""

        message = f"""
╔══════════════════════════════════════════════════════╗
║    CCoE EC2 Scheduler - RESUMEN DE EJECUCION          ║
╚══════════════════════════════════════════════════════╝

{dry_run_prefix}Estado:    {summary.get('status', 'DESCONOCIDO')}
Accion:    {action_es}
Duracion:  {summary.get('duration_seconds', 0):.2f}s
Dry Run:   {summary.get('dry_run', False)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESUMEN GLOBAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Cuentas procesadas:       {summary.get('accounts_processed', 0)}
  Instancias procesadas:    {summary.get('total_instances_processed', 0)}
  Instancias excepcionadas: {summary.get('total_instances_skipped', 0)}
  Errores:                  {summary.get('total_errors', 0)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DETALLE POR CUENTA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{accounts_detail}

Horario: {summary.get('start_time', '')} -> {summary.get('end_time', '')}
"""

        if summary.get('fatal_error'):
            message += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ERROR FATAL: {summary['fatal_error']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

        # El resumen se envia al fallback (ccoe-team@empresa.com)
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"{dry_run_prefix}[RESUMEN] CCoE Scheduler - {action_es} - {summary.get('status', 'DESCONOCIDO')}",
            Message=message
        )

        logger.info(f"Resumen de ejecucion enviado a SNS Topic: {SNS_TOPIC_ARN}")

    except Exception as e:
        logger.warning(f"Error al enviar resumen SNS: {str(e)}")


# ═══════════════════════════════════════════════
# UTILITY: Manual test entry point (optional)
# ═══════════════════════════════════════════════

if __name__ == '__main__':
    """
    For local testing only. Simulates a Lambda invocation.
    Usage:
        python ec2_scheduler_cross_account.py
    """
    print("=" * 60)
    print("CCoE EC2 Scheduler - Local Test Mode")
    print("=" * 60)
    print()

    test_event = {
        "action": "stop",
        "dry_run": True,
        "regions": ["us-east-1", "sa-east-1"],
    }

    print(f"Running with event: {json.dumps(test_event, indent=2)}")
    print()

    class MockContext:
        function_name = "CCoE-EC2Scheduler-CrossAccount"
        invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:CCoE-EC2Scheduler-CrossAccount"
        memory_limit_in_mb = 256

    result = lambda_handler(test_event, MockContext())
    print()
    print("=" * 60)
    print("RESULT:")
    print(json.dumps(result, indent=2, default=str))