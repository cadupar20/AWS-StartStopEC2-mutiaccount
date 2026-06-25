# CCoE EC2 Scheduler - Cross-Account, Cross-Region

Lambda function en Python que **apaga y enciende instancias EC2** en todas las cuentas de AWS Organizations, en múltiples regiones, respetando el modelo **Opt-Out** basado en tags definido por el CCoE.

---

## 📋 Requisitos

- Python 3.12+
- Terraform 1.5+ (o AWS CLI para CloudFormation StackSets)
- **Cuenta Management de AWS Control Tower** (donde se instalará la Lambda)
- AWS Organizations con cuentas miembro activas
- Permisos de **AdministratorAccess** en la cuenta Management para el deploy inicial
- (Opcional) Topic SNS para notificaciones

---

## 🏗️ Arquitectura

```
┌──────────────────────────────────────────────────────┐
│         Cuenta Management (Control Tower)             │
│                                                       │
│  ┌─────────────────┐     ┌────────────────────────┐  │
│  │ EventBridge     │────►│ Lambda Function         │  │
│  │ Scheduler       │     │ Python 3.12             │  │
│  │ (cron: stop)    │     │ ec2_scheduler_cross_    │  │
│  │ (cron: start)   │     │ account.py              │  │
│  └─────────────────┘     └───────────┬────────────┘  │
│                                      │                │
│                           STS:AssumeRole               │
│                           CCoE-EC2Scheduler-Role       │
└──────────────────────────┼───────────────────────────┘
                           │
    ┌──────────────────────┼──────────────────────┐
    │                      │                      │
    ▼                      ▼                      ▼
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ Cuenta A     │   │ Cuenta B     │   │ Cuenta C     │
│ (Sandbox)    │   │ (Desarrollo) │   │ (Sandbox)    │
│              │   │              │   │              │
│ Rol:         │   │ Rol:         │   │ Rol:         │
│ CCoE-EC2-    │   │ CCoE-EC2-    │   │ CCoE-EC2-    │
│ Scheduler-   │   │ Scheduler-   │   │ Scheduler-   │
│ Role         │   │ Role         │   │ Role         │
│              │   │              │   │              │
│ EC2 en      │   │ EC2 en      │   │ EC2 en      │
│ us-east-1   │   │ sa-east-1   │   │ us-west-2   │
│ us-east-2   │   │ us-east-1   │   │ eu-west-1   │
└──────────────┘   └──────────────┘   └──────────────┘
```

### Flujo de ejecución

1. **EventBridge Scheduler** ejecuta la Lambda según expresión cron (cada día hábil)
2. **Lambda** descubre todas las cuentas activas via `organizations:ListAccounts`
3. Por cada cuenta, **asume el rol** `CCoE-EC2Scheduler-Role` via STS
4. En cada cuenta/región, **descubre instancias EC2** con tag `environment` ∈ {sandbox, dev, desarrollo}
5. **Evalúa tags de excepción** (`no-shutdown`, `no-shutdown-reason`, `no-shutdown-approved-by`, `no-shutdown-expiry`)
6. **Ejecuta Stop/Start** según corresponda
7. **Envía notificaciones SNS** por cada acción individual + resumen final

---

## Flujo de Ejecución con Funciones y Parámetros

```
EventBridge Scheduler
  │
  ▼
lambda_handler(event, context)
  │   event = {"action": "stop|start", "dry_run": bool,
  │            "regions": [...], "accounts": [...]}
  │
  ├─► [Si no vienen cuentas en el event]
  │     _discover_organization_accounts()
  │       └─ sin parámetros
  │       └─ retorna: ["111111111111", "222222222222", ...]
  │
  ├─► [Si no vienen regiones en el event]
  │     _parse_regions()
  │       └─ sin parámetros (lee variable REGIONS)
  │       └─ retorna: ["us-east-1", "sa-east-1", ...]
  │
  └─► Por cada account_id (que no esté en EXCLUDED_ACCOUNTS):
        │
        ▼
        _process_account(account_id, action, dry_run, regions)
          │
          ├─► _get_account_email(account_id)
          │     └─ account_id: "111111111111"
          │     └─ retorna: "responsable@empresa.com" o fallback
          │
          ├─► sts_client.assume_role(RoleArn, RoleSessionName)  [boto3 directo]
          │     └─ retorna: credentials {AccessKeyId, SecretAccessKey, SessionToken}
          │
          └─► Por cada region en regions:
                │
                ▼
                _process_region(credentials, account_id, region,
                                action, dry_run, referente_email)
                  │
                  ├─► _discover_instances(ec2_client, target_states, environments)
                  │     ├─ ec2_client:    boto3 EC2 con credenciales del rol asumido
                  │     ├─ target_states: ["running"]  si action="stop"
                  │     │                ["stopped"]   si action="start"
                  │     ├─ environments:  ["sandbox", "desarrollo", ...]
                  │     └─ retorna: lista de instancias EC2 que coinciden
                  │
                  └─► Por cada instancia encontrada:
                        │
                        ├─► _parse_tags(instance["Tags"])
                        │     └─ tags_list: [{Key: "Name", Value: "mi-server"}, ...]
                        │     └─ retorna: {"Name": "mi-server", "environment": "sandbox"}
                        │
                        ├─► _environments_match(target_env, instance_env)
                        │     ├─ target_env:  "dev"
                        │     ├─ instance_env: "desarrollo"
                        │     └─ retorna: True / False
                        │
                        ├─► _evaluate_exception(tags, today)
                        │     ├─ tags:  {"no-shutdown": "true",
                        │     │          "no-shutdown-reason": "proyecto critico",
                        │     │          "no-shutdown-approved-by": "jefe@empresa.com",
                        │     │          "no-shutdown-expiry": "2026-12-31"}
                        │     ├─ today: "2026-06-22"
                        │     └─ retorna: {is_valid: bool, reason, approved_by, expiry_date}
                        │
                        ├─► [Si is_valid=True → instancia excepcionada]
                        │     _send_notification(..., action_label="EXCEPCION", ...)
                        │
                        ├─► [Si is_valid=False y dry_run=False]
                        │     ec2_client.stop_instances / start_instances  [boto3 directo]
                        │     _send_notification(..., action_label="APAGADO|ENCENDIDO", ...)
                        │       ├─ account_id:      "111111111111"
                        │       ├─ region:          "us-east-1"
                        │       ├─ instance_id:     "i-0abc123"
                        │       ├─ instance_name:   "mi-server"
                        │       ├─ environment:     "sandbox"
                        │       ├─ action_label:    "APAGADO" | "ENCENDIDO" | "EXCEPCION" | "ERROR"
                        │       ├─ dry_run:         False
                        │       ├─ referente_email: "responsable@empresa.com"
                        │       ├─ message_body:    texto del mensaje
                        │       └─ subject_line:    asunto del email
                        │
                        └─► [Si ocurre un error en stop/start]
                              _send_notification(..., action_label="ERROR", ...)
                                └─ mismos parámetros, con error en message_body

  │
  └─► Al finalizar todas las cuentas:
        _send_summary_notification(summary, action)
          ├─ summary: {accounts_processed, total_instances_processed,
          │            total_instances_skipped, total_errors,
          │            results_by_account, status, duration_seconds, ...}
          └─ action:  "stop" | "start"
```

### Estructura de iteración (3 niveles):

Flujo completo (sección "Flujo de Ejecución con Funciones y Parámetros"):


1	🏢 Organization → Accounts

2	📋 Account → Regions

3	🌐 Region → Instances

```
Organization (ListAccounts)
  └── Cuenta 1
       ├── _get_account_email() → email del Referente
       ├── sts.assume_role() → credenciales
       └── Region us-east-1
            ├── _discover_instances() → [instancias con tag environment]
            └── Instancia i-xxx
                 ├── _evaluate_exception() → ¿excepción válida?
                 ├── stop/start instances
                 └── _send_notification() → SNS
```

### Helper

`_action_text(action)` es llamado dentro de `_process_region` y `_send_summary_notification` para convertir el texto:

```
_action_text("stop")  → "APAGADO"
_action_text("start") → "ENCENDIDO"
```

## 🎯 Modelo Opt-Out

Todas las instancias EC2 con tag `environment` en `Sandbox`, `Dev` o `Desarrollo` se **apagan automáticamente** fuera del horario laboral, **a menos que** tengan una excepción válida.

### Tags de excepción (obligatorios los 4)

| Tag | Descripción | Ejemplo |
|-----|------------|---------|
| `no-shutdown` | Indica que la instancia NO debe apagarse | `true` |
| `no-shutdown-reason` | Justificación de la excepción | `"Job ETL nocturno hasta 30/06"` |
| `no-shutdown-approved-by` | Email del aprobador | `owner@empresa.com` |
| `no-shutdown-expiry` | Fecha de vencimiento (YYYY-MM-DD) | `2026-07-15` |

> ⚠️ **Todas las condiciones deben cumplirse**. Si falta algún tag o está vencido, la instancia se apaga igual.

### Horarios programados

| Ambiente | Encendido (ART) | Apagado (ART) | Días |
|----------|----------------|---------------|------|
| Desarrollo | 08:00 | 20:00 | Lun-Vie |
| Sandbox | 09:00 | 19:00 | Lun-Vie |

---

## 📁 Estructura del proyecto

```
StartStopEc2/
├── src/
│   └── ec2_scheduler_cross_account.py   # Lambda function principal
├── iam/
│   ├── management-account-policy.json    # Policy para cuenta management
│   ├── member-account-policy.json        # Policy para cuentas miembro
│   └── member-account-trust-policy.json  # Trust policy para el rol
├── terraform/
│   ├── management-account/
│   │   └── main.tf                       # Deploy en cuenta Control Tower
│   └── member-account/
│       └── main.tf                       # Deploy en cada cuenta miembro
├── cloudformation/
│   └── member-account-role.yaml          # StackSets para cuentas miembro
└── README.md
```

---

## 🔧 Guía de Implementación Paso a Paso

### FASE 0: Prerequisitos en la Cuenta Management (Control Tower)

Antes de empezar, necesitas tener acceso a la **cuenta Management** de AWS Control Tower con los siguientes permisos mínimos:

#### Permisos necesarios en la cuenta Management

| Servicio | Permiso | Propósito |
|----------|---------|-----------|
| AWS Organizations | `organizations:ListAccounts` | Descubrir todas las cuentas de la org |
| AWS Organizations | `organizations:DescribeOrganization` | Validar configuración de la org |
| IAM | `iam:CreateRole` | Crear rol para la Lambda |
| IAM | `iam:CreatePolicy` | Crear política de permisos |
| IAM | `iam:AttachRolePolicy` | Asociar política al rol |
| IAM | `iam:PassRole` | Pasar rol a Lambda y EventBridge |
| Lambda | `lambda:CreateFunction` | Crear la función Lambda |
| Lambda | `lambda:InvokeFunction` | Invocar la Lambda |
| EventBridge Scheduler | `scheduler:CreateSchedule` | Crear schedules de apagado/encendido |
| CloudWatch Logs | `logs:CreateLogGroup` | Grupo de logs para la Lambda |
| SNS | `sns:CreateTopic` | (Opcional) Crear topic de notificaciones |
| SNS | `sns:Publish` | Publicar notificaciones |
| STS | `sts:AssumeRole` | Asumir rol en cuentas miembro |
| CloudFormation | `cloudformation:CreateStackSet` | (Opcional) Para StackSets |
| CloudFormation | `cloudformation:CreateStackInstances` | (Opcional) Para StackSets |

> **Tip**: Si estás haciendo el deploy inicial, podés usar temporalmente `AdministratorAccess` y luego acotar permisos.

---

### FASE 1: Crear el Rol IAM en cada Cuenta Miembro

Este es el rol que la Lambda va a asumir para poder ejecutar acciones EC2 en cada cuenta.

#### Opción A: CloudFormation StackSets (RECOMENDADO - Automático)

Desde la **cuenta Management (Control Tower)**, ejecutá estos comandos:

```bash
# 1. ID de tu cuenta Management
export MANAGEMENT_ACCOUNT_ID="123456789012"  # ← REEMPLAZAR CON TU ID

# 2. IDs de las Organizational Units donde están las cuentas Sandbox/Desarrollo
export TARGET_OU="ou-xxxx-xxxxxxxxx"  # ← REEMPLAZAR

# 3. Crear el StackSet (solo una vez)
aws cloudformation create-stack-set \
    --stack-set-name CCoE-EC2Scheduler-Role \
    --template-body file://cloudformation/member-account-role.yaml \
    --parameters ParameterKey=ManagementAccountId,ParameterValue=$MANAGEMENT_ACCOUNT_ID \
    --capabilities CAPABILITY_NAMED_IAM \
    --permission-model SERVICE_MANAGED \
    --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false

# 4. Deployar a todas las cuentas de la OU especificada
aws cloudformation create-stack-instances \
    --stack-set-name CCoE-EC2Scheduler-Role \
    --deployment-targets OrganizationalUnitIds=["$TARGET_OU"] \
    --regions ["us-east-1"]

# 5. Verificar que se crearon los stacks
aws cloudformation list-stack-instances \
    --stack-set-name CCoE-EC2Scheduler-Role
```

**¿Qué hace esto?**
- Crea el rol `CCoE-EC2Scheduler-Role` en **cada cuenta** de la OU destino
- El rol confía en la cuenta Management (ExternalID: `CCoE-EC2Scheduler`)
- El rol solo permite detener/iniciar EC2 con tag `environment` ∈ {sandbox, dev, desarrollo}
- Si se agregan nuevas cuentas a la OU, el rol se crea **automáticamente**

#### Opción B: Manual (Cuenta por cuenta)

Si preferís hacerlo manualmente en cada cuenta:

```bash
# En la cuenta miembro destino:
aws cloudformation create-stack \
    --stack-name CCoE-EC2Scheduler-Role \
    --template-body file://cloudformation/member-account-role.yaml \
    --parameters ParameterKey=ManagementAccountId,ParameterValue=123456789012 \
    --capabilities CAPABILITY_NAMED_IAM
```

#### Opción C: Terraform (por cuenta)

```bash
cd terraform/member-account
terraform init
terraform plan -var="management_account_id=123456789012"
terraform apply
```

#### ✅ Verificar que el rol se creó correctamente

Podés verificar desde la cuenta Management que podes asumir el rol:

```bash
# Probar asumir rol en una cuenta miembro de prueba
aws sts assume-role \
    --role-arn "arn:aws:iam::ACCOUNT_ID_MIEMBRO:role/CCoE-EC2Scheduler-Role" \
    --role-session-name "Verification" \
    --external-id "CCoE-EC2Scheduler"
```

Si el comando devuelve `Credentials`, el rol está funcionando correctamente.

---

### FASE 2: Crear el Topic SNS para Notificaciones (Opcional)

Si querés recibir notificaciones por cada acción de apagado/encendido:

```bash
# Crear el topic SNS
aws sns create-topic --name CCoE-EC2Scheduler-Notifications

# Anotar el ARN que devuelve (ej: arn:aws:sns:us-east-1:123456789012:CCoE-EC2Scheduler-Notifications)

# Suscribir los emails que recibirán las notificaciones
aws sns subscribe \
    --topic-arn "arn:aws:sns:us-east-1:123456789012:CCoE-EC2Scheduler-Notifications" \
    --protocol email \
    --notification-endpoint "ccoe-team@empresa.com"

# Repetir para cada destinatario
aws sns subscribe \
    --topic-arn "arn:aws:sns:us-east-1:123456789012:CCoE-EC2Scheduler-Notifications" \
    --protocol email \
    --notification-endpoint "finops@empresa.com"
```

> ⚠️ **Importante**: Cada suscriptor por email debe confirmar la suscripción desde el email que recibe de AWS SNS.

---

### FASE 3: Deployar la Lambda y Schedules en la Cuenta Management

#### Opción A: Terraform (RECOMENDADO)

```bash
cd terraform/management-account

# Inicializar Terraform
terraform init

# Ver el plan (reemplazar con tu SNS Topic ARN)
terraform plan \
    -var="sns_topic_arn=arn:aws:sns:us-east-1:123456789012:CCoE-EC2Scheduler-Notifications" \
    -var="regions=us-east-1,us-east-2,sa-east-1,eu-west-1"

# Aplicar
terraform apply \
    -var="sns_topic_arn=arn:aws:sns:us-east-1:123456789012:CCoE-EC2Scheduler-Notifications"
```

**¿Qué crea Terraform?**
| Recurso | Nombre | Propósito |
|---------|--------|-----------|
| IAM Role | `CCoE-EC2Scheduler-Management-Role` | Rol de ejecución de la Lambda |
| IAM Policy | `CCoE-EC2Scheduler-Management-Policy` | Permisos: Organizations + STS + SNS + Logs |
| Lambda | `CCoE-EC2Scheduler-CrossAccount` | Función Lambda con Python 3.12 |
| IAM Role | `CCoE-EC2Scheduler-EventBridge-Role` | Rol para que EventBridge invoque la Lambda |
| Schedule | `CCoE-Stop-Desarrollo` | Apagado Lun-Vie 20:00 ART |
| Schedule | `CCoE-Stop-Sandbox` | Apagado Lun-Vie 19:00 ART |
| Schedule | `CCoE-Start-Desarrollo` | Encendido Lun-Vie 08:00 ART |
| Schedule | `CCoE-Start-Sandbox` | Encendido Lun-Vie 09:00 ART |
| Log Group | `/aws/lambda/CCoE-EC2Scheduler-CrossAccount` | Logs con retención de 30 días |
| Alarm | `CCoE-EC2Scheduler-Errors` | Alarma si la Lambda tiene errores |

#### Opción B: AWS Console (Manual)

Si preferís hacerlo desde la consola AWS:

1. **Ir a Lambda** → Crear función → "Author from scratch"
2. **Nombre**: `CCoE-EC2Scheduler-CrossAccount`
3. **Runtime**: Python 3.12
4. **Rol**: Crear rol básico con permisos Lambda (luego lo actualizamos)
5. **Subir código**: Copiar el contenido de `src/ec2_scheduler_cross_account.py`
6. **Timeout**: 300 segundos
7. **Memoria**: 256 MB
8. **Variables de entorno**:
   | Variable | Valor |
   |----------|-------|
   | `ENVIRONMENTS` | `sandbox,desarrollo` |
   | `REGIONS` | `us-east-1,us-east-2,sa-east-1` |
   | `SNS_TOPIC_ARN` | `arn:aws:sns:...` |
9. **Rol IAM**: Adjuntar la política de `iam/management-account-policy.json`
10. **EventBridge**: Crear 4 reglas (ver sección Schedules)

---

### FASE 4: Configurar Variables de Entorno de la Lambda

Una vez creada la Lambda, configurar estas variables según tu entorno:

| Variable | Valor Recomendado | Explicación |
|----------|-------------------|-------------|
| `ENVIRONMENTS` | `sandbox,desarrollo` | Ambientes a gestionar. Acepta: sandbox, dev, desarrollo |
| `REGIONS` | `us-east-1,us-east-2,sa-east-1` | Regiones donde buscar EC2. Usar `all` para todas |
| `SNS_TOPIC_ARN` | `arn:aws:sns:...` | ARN del topic SNS creado en Fase 2 |
| `ASSUME_ROLE_NAME` | `CCoE-EC2Scheduler-Role` | Debe coincidir con el rol creado en Fase 1 |
| `EXCLUDED_ACCOUNTS` | (vacío o IDs separados por coma) | Cuentas a excluir (ej: la management) |
| `DRY_RUN` | `false` | Poner `true` SOLO para pruebas iniciales |

---

### FASE 5: Probar la Lambda en Modo Dry-Run

Antes de ponerlo en producción, ejecutar una prueba en **modo simulación**:

```bash
# Desde la consola AWS: Lambda → Test
# Crear un evento de prueba con:
{
    "action": "stop",
    "dry_run": true
}

# O desde CLI:
aws lambda invoke \
    --function-name CCoE-EC2Scheduler-CrossAccount \
    --payload '{"action":"stop","dry_run":true}' \
    --cli-binary-format raw-in-base64-out \
    response.json

# Ver resultado
cat response.json
```

**¿Qué esperar?**
- La Lambda va a **descubrir** todas las cuentas, regiones e instancias
- **NO** va a apagar nada (dry_run=true)
- Va a registrar en CloudWatch Logs qué instancias **hubiera** apagado
- Podés ver los logs en: CloudWatch → Log groups → `/aws/lambda/CCoE-EC2Scheduler-CrossAccount`

#### Verificar logs de la prueba

```bash
# Obtener el último log stream
LOG_STREAM=$(aws logs describe-log-streams \
    --log-group-name /aws/lambda/CCoE-EC2Scheduler-CrossAccount \
    --order-by LastEventTime \
    --descending \
    --max-items 1 \
    --query 'logStreams[0].logStreamName' \
    --output text)

# Ver los logs
aws logs get-log-events \
    --log-group-name /aws/lambda/CCoE-EC2Scheduler-CrossAccount\
    --log-stream-name "$LOG_STREAM"
```

---

### FASE 6: Activar la Ejecución Automática

Una vez verificada la prueba dry-run:

1. **En la Lambda**, cambiar la variable `DRY_RUN` a `false`
2. **Verificar** que los 4 schedules de EventBridge están **ENABLED** (deberían estarlo por defecto)
3. **Esperar** al próximo horario programado, o **forzar una ejecución manual**:

```bash
# Forzar apagado manual (DRY_RUN=false)
aws lambda invoke \
    --function-name CCoE-EC2Scheduler-CrossAccount \
    --payload '{"action":"stop"}' \
    --cli-binary-format raw-in-base64-out \
    response.json

# Forzar encendido manual
aws lambda invoke \
    --function-name CCoE-EC2Scheduler-CrossAccount \
    --payload '{"action":"start"}' \
    --cli-binary-format raw-in-base64-out \
    response.json
```

---

## 📊 Políticas IAM - Resumen Completo

### Política para la Cuenta Management (Control Tower)

Esta política se asigna al **Rol de la Lambda** en la cuenta Management:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "OrganizationsListAccounts",
            "Effect": "Allow",
            "Action": [
                "organizations:ListAccounts",
                "organizations:DescribeOrganization"
            ],
            "Resource": "*"
        },
        {
            "Sid": "STSAssumeRoleCrossAccount",
            "Effect": "Allow",
            "Action": ["sts:AssumeRole"],
            "Resource": ["arn:aws:iam::*:role/CCoE-EC2Scheduler-Role"]
        },
        {
            "Sid": "SNSPublishNotifications",
            "Effect": "Allow",
            "Action": ["sns:Publish"],
            "Resource": "arn:aws:sns:*:*:CCoE-EC2Scheduler-*"
        },
        {
            "Sid": "CloudWatchLogs",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "arn:aws:logs:*:*:*"
        },
        {
            "Sid": "EC2DescribeRegions",
            "Effect": "Allow",
            "Action": ["ec2:DescribeRegions"],
            "Resource": "*"
        }
    ]
}
```

### Política para cada Cuenta Miembro

Esta política se asigna al **Rol `CCoE-EC2Scheduler-Role`** en cada cuenta miembro:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "EC2DescribeInstances",
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeInstances",
                "ec2:DescribeInstanceStatus",
                "ec2:DescribeTags",
                "ec2:DescribeRegions"
            ],
            "Resource": "*"
        },
        {
            "Sid": "EC2ManageInstances",
            "Effect": "Allow",
            "Action": [
                "ec2:StopInstances",
                "ec2:StartInstances"
            ],
            "Resource": "*",
            "Condition": {
                "StringEqualsIgnoreCase": {
                    "aws:ResourceTag/environment": ["sandbox", "dev", "desarrollo"]
                }
            }
        }
    ]
}
```

### Trust Policy del Rol en Cuenta Miembro

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "AWS": "arn:aws:iam::MANAGEMENT_ACCOUNT_ID:root"
            },
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {
                    "sts:ExternalId": "CCoE-EC2Scheduler"
                }
            }
        }
    ]
}
```

---

## ⚙️ Variables de Entorno (Lambda)

| Variable | Default | Descripción |
|----------|---------|-------------|
| `ENVIRONMENTS` | `sandbox,desarrollo` | Ambientes a gestionar (separados por coma) |
| `REGIONS` | `us-east-1,us-east-2,...` | Regiones, o `all` para todas las habilitadas |
| `ACTION` | `stop` | Acción por defecto (`stop` o `start`) |
| `DRY_RUN` | `false` | Modo simulación (no hace cambios reales) |
| `SNS_TOPIC_ARN` | (vacío) | ARN del topic SNS para notificaciones |
| `ASSUME_ROLE_NAME` | `CCoE-EC2Scheduler-Role` | Nombre del rol IAM en cada cuenta miembro |
| `EXCLUDED_ACCOUNTS` | (vacío) | IDs de cuentas a excluir (ej: la cuenta management) |

---

## 🔍 Uso del Event Payload

La Lambda acepta un payload opcional que sobrescribe las variables de entorno:

```json
{
    "action": "stop",
    "dry_run": true,
    "regions": ["us-east-1", "sa-east-1"],
    "accounts": ["111111111111", "222222222222"]
}
```

Esto permite:
- **Probar** con `dry_run: true` antes del deploy real
- **Ejecutar manualmente** sobre cuentas o regiones específicas
- **Forzar** una acción diferente a la programada

---

## 📊 Notificaciones SNS

La función envía dos tipos de notificaciones:

### Por instancia
Se envía cada vez que una instancia es apagada, encendida o excepcionada.
```
🛑 CCoE EC2 Scheduler - Instance STOP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Account:     123456789012
Region:      us-east-1
Instance ID: i-0abc123def456
Instance:    web-server-dev-01
Environment: desarrollo
Action:      STOP
Reason:      Instancia detenida automáticamente por CCoE Scheduler
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Resumen de ejecución
Al finalizar, envía un resumen con:
- Cuentas procesadas
- Instancias procesadas vs excepcionadas
- Errores
- Duración de la ejecución

---

## 🧪 Modo Dry-Run

Configurar `DRY_RUN=true` para simular la ejecución sin realizar cambios reales:

```bash
# En Lambda environment variables
DRY_RUN=true

# O via event payload
{
    "action": "stop",
    "dry_run": true
}
```

---

## 🔐 Seguridad

### Principio de mínimo privilegio
- El rol en cuentas miembro solo permite `StopInstances`/`StartInstances` en instancias con tag `environment` específico
- La Lambda en la cuenta management solo puede asumir roles con nombre `CCoE-EC2Scheduler-Role`
- External ID (`sts:ExternalId`) protege contra el **confused deputy problem**
- La política de la Lambda está acotada a los servicios que necesita (Organizations, STS, SNS, Logs)

### External ID - Confused Deputy Protection
El `sts:ExternalId` es un mecanismo de seguridad que:
- Evita que un atacante en otra cuenta use este rol
- Asegura que solo la cuenta Management con el ExternalID correcto puede asumir el rol
- Es una práctica recomendada por AWS para delegación entre cuentas

### Restricción por tags
```json
"Condition": {
    "StringEqualsIgnoreCase": {
        "aws:ResourceTag/environment": ["sandbox", "dev", "desarrollo"]
    }
}
```
Esto asegura que aunque alguien obtenga acceso al rol, solo podrá detener/iniciar instancias con esos tags específicos.

---

## ⏱️ Schedules (EventBridge Scheduler)

| Nombre | Expresión Cron | Hora ART | Acción | Ambiente |
|--------|---------------|----------|--------|----------|
| `CCoE-Stop-Desarrollo` | `cron(0 23 ? * MON-FRI *)` | 20:00 | STOP | Desarrollo |
| `CCoE-Stop-Sandbox` | `cron(0 22 ? * MON-FRI *)` | 19:00 | STOP | Sandbox |
| `CCoE-Start-Desarrollo` | `cron(0 11 ? * MON-FRI *)` | 08:00 | START | Desarrollo |
| `CCoE-Start-Sandbox` | `cron(0 12 ? * MON-FRI *)` | 09:00 | START | Sandbox |

> ⚠️ **Importante**: Las expresiones cron están en **zona horaria ART** (America/Argentina/Buenos_Aires).
> No es necesario convertir a UTC porque EventBridge Scheduler soporta configuración de zona horaria.

---

## 🐛 Troubleshooting

### La Lambda falla con "Access Denied" al asumir rol

```bash
# Posibles causas y soluciones:

# 1. El rol no existe en la cuenta miembro
#    → Verificar con:
aws iam get-role --role-name CCoE-EC2Scheduler-Role --profile cuenta-miembro

# 2. La trust policy tiene mal el ID de cuenta management
#    → Verificar con:
aws iam get-role --role-name CCoE-EC2Scheduler-Role --profile cuenta-miembro --query Role.AssumeRolePolicyDocument

# 3. ExternalId no coincide
#    → Verificar que en la trust policy dice "sts:ExternalId": "CCoE-EC2Scheduler"
```

### No se procesan instancias

```bash
# Verificar:
# 1. Tienen el tag environment con valor correcto
aws ec2 describe-instances \
    --instance-ids i-xxxxx \
    --query 'Reservations[0].Instances[0].Tags' \
    --profile cuenta-miembro

# 2. La región está incluida en REGIONS
# 3. Si ACTION=stop, las instancias deben estar running
aws ec2 describe-instances \
    --instance-ids i-xxxxx \
    --query 'Reservations[0].Instances[0].State.Name' \
    --profile cuenta-miembro
```

### No llegan notificaciones SNS

```bash
# Verificar:
# 1. SNS_TOPIC_ARN está configurado
# 2. La política del topic permite publicar desde la Lambda
aws sns get-topic-attributes \
    --topic-arn "arn:aws:sns:..."

# 3. Las suscripciones están confirmadas
aws sns list-subscriptions-by-topic \
    --topic-arn "arn:aws:sns:..."
```

### La Lambda da timeout

```bash
# Aumentar el timeout en la configuración de Lambda
# Valor recomendado: 300 segundos (5 minutos)
# Para organizaciones grandes con muchas cuentas/regiones: 600 segundos
```

---

## 📄 Licencia

MIT - CCoE Engineering