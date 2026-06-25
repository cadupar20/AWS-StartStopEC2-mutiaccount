# 1st Step — Guía de Implementación CCoE EC2 Scheduler

> **Objetivo:** Apagar y encender instancias EC2 automáticamente en todas las cuentas de AWS Organizations, respetando el modelo Opt-Out con tags.

---

## Antes de empezar — Información que necesitás tener a mano

Completá estos valores. Los vas a usar en todos los pasos siguientes.

| Dato | Descripción | Tu valor |
|------|-------------|----------|
| `MANAGEMENT_ACCOUNT_ID` | ID de 12 dígitos de tu cuenta Management / Control Tower | `____________` |
| `TARGET_OU_ID` | ID de la OU donde están las cuentas miembro (ej: `ou-xxxx-xxxxxxxx`) | `____________` |
| `SNS_EMAIL_FALLBACK` | Email del equipo CCoE para notificaciones generales | `____________` |
| `REGIONS` | Regiones donde querés actuar (ej: `us-east-1,sa-east-1`) | `____________` |
| `EXCLUDED_ACCOUNTS` | IDs de cuentas a omitir, separados por coma (mínimo: la Management) | `____________` |

---

## Pre-requisitos

### Herramientas requeridas

| Herramienta | Versión mínima | Verificar |
|-------------|---------------|-----------|
| AWS CLI | v2 | `aws --version` |
| Terraform | 1.5+ | `terraform -version` |
| Python | 3.12+ | `python --version` |

### Configuración de AWS CLI

Tu sesión de AWS CLI debe apuntar a la **cuenta Management** antes de ejecutar cualquier comando.

```bash
# Verificar que apunta a la cuenta correcta
aws sts get-caller-identity

# La respuesta debe mostrar tu MANAGEMENT_ACCOUNT_ID en "Account":
# {
#     "Account": "123456789012",   ← DEBE ser tu Management Account
#     "UserId": "...",
#     "Arn": "arn:aws:iam::123456789012:..."
# }
```

Si no es la Management Account, configurá el perfil correcto:

```bash
# Opción A: variable de entorno
export AWS_PROFILE=nombre-perfil-management

# Opción B: flags explícitos en cada comando
aws <comando> --profile nombre-perfil-management --region us-east-1
```

---

## ¿Dónde se despliega cada cosa?

```
┌─────────────────────────────────────────────────────┐
│          CUENTA MANAGEMENT (Control Tower)           │
│                                                      │
│  FASE 2 → IAM Role Lambda (Management)              │
│  FASE 3 → Lambda Function                           │
│  FASE 3 → EventBridge Schedules (4 reglas)          │
│  FASE 3 → CloudWatch Log Group + Alarm              │
│  FASE 2 → SNS Topic (opcional)                      │
└──────────────────────┬──────────────────────────────┘
                       │ STS:AssumeRole
          ─────────────┼─────────────
         │             │             │
         ▼             ▼             ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│  Cuenta A    │ │  Cuenta B    │ │  Cuenta C    │
│              │ │              │ │              │
│  FASE 1 →   │ │  FASE 1 →   │ │  FASE 1 →   │
│  IAM Role   │ │  IAM Role   │ │  IAM Role   │
│  (miembro)  │ │  (miembro)  │ │  (miembro)  │
└──────────────┘ └──────────────┘ └──────────────┘
```

---

## FASE 1 — Crear el Rol IAM en cada Cuenta Miembro

> **Dónde:** Se ejecuta desde la **cuenta Management**, pero crea recursos en cada cuenta miembro via CloudFormation StackSets.
>
> **Qué crea:** El rol `CCoE-EC2Scheduler-Role` en cada cuenta miembro. La Lambda lo asume para poder operar EC2.

### Opción A: CloudFormation StackSets (Recomendada)

Es la opción recomendada porque si se agregan nuevas cuentas a la OU, el rol se despliega automáticamente.

```bash
# Reemplazar los valores antes de ejecutar
MANAGEMENT_ACCOUNT_ID="____________"   # ← Tu Management Account ID
TARGET_OU="____________"               # ← ID de la OU destino (ou-xxxx-xxxxxxxx)

# Paso 1: Crear el StackSet (solo se ejecuta una vez)
aws cloudformation create-stack-set \
    --stack-set-name CCoE-EC2Scheduler-Role \
    --template-body file://cloudformation/member-account-role.yaml \
    --parameters \
        ParameterKey=ManagementAccountId,ParameterValue=$MANAGEMENT_ACCOUNT_ID \
    --capabilities CAPABILITY_NAMED_IAM \
    --permission-model SERVICE_MANAGED \
    --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false

# Paso 2: Deployar a todas las cuentas de la OU
aws cloudformation create-stack-instances \
    --stack-set-name CCoE-EC2Scheduler-Role \
    --deployment-targets OrganizationalUnitIds=["$TARGET_OU"] \
    --regions ["us-east-1"]

# Paso 3: Verificar el estado del deploy
aws cloudformation list-stack-instances \
    --stack-set-name CCoE-EC2Scheduler-Role
```

Esperar hasta que todas las instancias muestren `CURRENT` en `StackInstanceStatus`.

### Opción B: Terraform (por cuenta individual)

Si preferís manejar el deploy de forma manual en una cuenta específica:

```bash
cd terraform/member-account

terraform init

# Reemplazar con el ID de la cuenta miembro destino
terraform plan -var="management_account_id=____________"
terraform apply -var="management_account_id=____________"
```

### ✅ Verificar que el rol funciona

Desde la cuenta Management, probar asumir el rol en una cuenta miembro:

```bash
# Reemplazar ACCOUNT_ID_MIEMBRO con el ID de una cuenta miembro
aws sts assume-role \
    --role-arn "arn:aws:iam::ACCOUNT_ID_MIEMBRO:role/CCoE-EC2Scheduler-Role" \
    --role-session-name "Verification" \
    --external-id "CCoE-EC2Scheduler"

# Si devuelve "Credentials": { "AccessKeyId": "...", ... } → ✅ OK
# Si devuelve AccessDenied → revisar la trust policy del rol
```

---

## FASE 2 — Crear el Topic SNS para Notificaciones (Opcional)

> **Dónde:** Cuenta Management.
>
> **Qué crea:** Un topic SNS al que la Lambda publicará notificaciones de apagado/encendido/errores.

```bash
# Reemplazar la región según donde quieras el topic
SNS_REGION="us-east-1"

# Crear el topic
aws sns create-topic \
    --name CCoE-EC2Scheduler-Notifications \
    --region $SNS_REGION

# ↑ Anotar el ARN que devuelve. Lo vas a necesitar en la Fase 3.
# Ejemplo: arn:aws:sns:us-east-1:123456789012:CCoE-EC2Scheduler-Notifications

# Suscribir el email del equipo CCoE (fallback)
aws sns subscribe \
    --topic-arn "arn:aws:sns:$SNS_REGION:____________:CCoE-EC2Scheduler-Notifications" \
    --protocol email \
    --notification-endpoint "____________"   # ← tu SNS_EMAIL_FALLBACK
```

> ⚠️ Cada email suscrito debe **confirmar la suscripción** desde el email que recibe de AWS antes de recibir notificaciones.

El ARN del topic para la Fase 3:

```
SNS_TOPIC_ARN = "arn:aws:sns:____________:____________:CCoE-EC2Scheduler-Notifications"
                              región          account-id
```

---

## FASE 3 — Deployar la Lambda en la Cuenta Management

> **Dónde:** Cuenta Management (Control Tower). Usar Terraform.
>
> **Qué crea:** Lambda + IAM Roles + EventBridge Schedules + CloudWatch Logs + Alarm.

```bash
cd terraform/management-account

# Inicializar Terraform
terraform init

# Ver el plan antes de aplicar
terraform plan \
    -var="sns_topic_arn=arn:aws:sns:____________:____________:CCoE-EC2Scheduler-Notifications" \
    -var="regions=____________" \
    -var="excluded_accounts=____________" \
    -var="sns_fallback_email=____________"

# Si el plan se ve bien, aplicar
terraform apply \
    -var="sns_topic_arn=arn:aws:sns:____________:____________:CCoE-EC2Scheduler-Notifications" \
    -var="regions=____________" \
    -var="excluded_accounts=____________" \
    -var="sns_fallback_email=____________"
```

**Ejemplo con valores reales:**

```bash
terraform apply \
    -var="sns_topic_arn=arn:aws:sns:us-east-1:123456789012:CCoE-EC2Scheduler-Notifications" \
    -var="regions=us-east-1,us-east-2,sa-east-1" \
    -var="excluded_accounts=123456789012" \
    -var="sns_fallback_email=ccoe-team@empresa.com"
```

**Recursos que Terraform crea:**

| Recurso | Nombre | Para qué sirve |
|---------|--------|----------------|
| IAM Role | `CCoE-EC2Scheduler-Management-Role` | Rol de ejecución de la Lambda |
| IAM Policy | `CCoE-EC2Scheduler-Management-Policy` | Permisos: Organizations + STS + SNS + Logs |
| Lambda | `CCoE-EC2Scheduler-CrossAccount` | Función principal (Python 3.12) |
| IAM Role | `CCoE-EC2Scheduler-EventBridge-Role` | Permite a EventBridge invocar la Lambda |
| Schedule | `CCoE-Stop-Desarrollo` | Apagado Lun-Vie 20:00 ART |
| Schedule | `CCoE-Stop-Sandbox` | Apagado Lun-Vie 19:00 ART |
| Schedule | `CCoE-Start-Desarrollo` | Encendido Lun-Vie 08:00 ART |
| Schedule | `CCoE-Start-Sandbox` | Encendido Lun-Vie 09:00 ART |
| Log Group | `/aws/lambda/CCoE-EC2Scheduler-CrossAccount` | Logs (30 días retención) |
| Alarm | `CCoE-EC2Scheduler-Errors` | Alerta si la Lambda tiene errores |

---

## FASE 4 — Verificar Variables de Entorno de la Lambda

Después del deploy, confirmar en la consola AWS (Lambda → Configuración → Variables de entorno) que los valores son correctos:

| Variable | Valor esperado |
|----------|---------------|
| `ENVIRONMENTS` | `sandbox,dev,desarrollo` |
| `REGIONS` | Las regiones que configuraste |
| `SNS_TOPIC_ARN` | El ARN del topic SNS de la Fase 2 |
| `ASSUME_ROLE_NAME` | `CCoE-EC2Scheduler-Role` |
| `EXCLUDED_ACCOUNTS` | El ID de tu Management Account (al menos) |
| `DRY_RUN` | `false` (para producción) |
| `SNS_FALLBACK_EMAIL` | El email del equipo CCoE |

---

## FASE 5 — Prueba en Modo Dry-Run

Antes de habilitar en producción, ejecutar una prueba sin cambios reales.

```bash
# Invocar la Lambda en modo simulación
aws lambda invoke \
    --function-name CCoE-EC2Scheduler-CrossAccount \
    --payload '{"action":"stop","dry_run":true}' \
    --cli-binary-format raw-in-base64-out \
    response.json

# Ver el resultado
type response.json
```

**¿Qué esperar?**

- La Lambda descubre todas las cuentas y regiones
- **No apaga nada** (dry_run = true)
- En CloudWatch Logs podés ver qué instancias hubiera apagado

**Revisar los logs:**

```bash
# Obtener el log stream más reciente
aws logs describe-log-streams \
    --log-group-name /aws/lambda/CCoE-EC2Scheduler-CrossAccount \
    --order-by LastEventTime \
    --descending \
    --max-items 1

# Ver los eventos del log stream (reemplazar LOG_STREAM_NAME)
aws logs get-log-events \
    --log-group-name /aws/lambda/CCoE-EC2Scheduler-CrossAccount \
    --log-stream-name "LOG_STREAM_NAME"
```

**Señales de que todo está bien:**

```
✅  "Descubiertas N cuentas desde Organizations"
✅  "Asumiendo rol: arn:aws:iam::XXXXXXXXXXXX:role/CCoE-EC2Scheduler-Role"
✅  "[DRY-RUN] Se apagaría [sandbox] mi-instancia (i-0abc123)"
✅  "Lambda completada en X.XXs"
```

**Señales de problema:**

```
❌  "Error al listar cuentas de Organization"      → Verificar permisos Organizations en Management
❌  "AccessDenied al asumir rol"                   → Verificar trust policy en cuenta miembro (Fase 1)
❌  "No se encontraron instancias en us-east-1"    → Verificar tags environment en las EC2
```

---

## FASE 6 — Activar en Producción

Una vez que el dry-run muestra los resultados esperados:

1. Cambiar la variable `DRY_RUN` a `false` en la Lambda
2. Verificar que los 4 schedules de EventBridge están en estado `ENABLED`
3. Ejecutar un test real (opcional, fuera del horario laboral):

```bash
# Forzar apagado manual real
aws lambda invoke \
    --function-name CCoE-EC2Scheduler-CrossAccount \
    --payload '{"action":"stop"}' \
    --cli-binary-format raw-in-base64-out \
    response.json

# Forzar encendido manual real
aws lambda invoke \
    --function-name CCoE-EC2Scheduler-CrossAccount \
    --payload '{"action":"start"}' \
    --cli-binary-format raw-in-base64-out \
    response.json
```

---

## Tags requeridos en instancias EC2

### Para que una instancia sea gestionada (apagada/encendida)

La instancia debe tener el tag:

| Tag | Valores válidos |
|-----|----------------|
| `environment` | `sandbox` / `dev` / `desarrollo` |

### Para que una instancia sea excepcionada (no se apaga)

Los 4 tags son obligatorios. Si falta uno o está vencido, la instancia se apaga igual.

| Tag | Descripción | Ejemplo |
|-----|-------------|---------|
| `no-shutdown` | Habilita la excepción | `true` |
| `no-shutdown-reason` | Justificación | `Job ETL nocturno hasta 30/06` |
| `no-shutdown-approved-by` | Email del aprobador | `owner@empresa.com` |
| `no-shutdown-expiry` | Fecha límite (YYYY-MM-DD) | `2026-07-15` |

### Para notificar al responsable correcto

Agregar este tag a nivel de **cuenta AWS** (no en la instancia) via AWS Organizations:

| Tag | Descripción | Ejemplo |
|-----|-------------|---------|
| `Referente` | Email del responsable de la cuenta | `equipo-dev@empresa.com` |

---

## Resumen de horarios

| Schedule | Expresión cron | Hora ART | Acción |
|----------|---------------|----------|--------|
| `CCoE-Stop-Desarrollo` | `cron(0 23 ? * MON-FRI *)` | 20:00 | STOP |
| `CCoE-Stop-Sandbox` | `cron(0 22 ? * MON-FRI *)` | 19:00 | STOP |
| `CCoE-Start-Desarrollo` | `cron(0 11 ? * MON-FRI *)` | 08:00 | START |
| `CCoE-Start-Sandbox` | `cron(0 12 ? * MON-FRI *)` | 09:00 | START |

> Los schedules usan zona horaria `America/Argentina/Buenos_Aires` directamente en EventBridge Scheduler.

---

## Permisos mínimos para ejecutar este deploy

### En la Cuenta Management (quien ejecuta el Terraform)

| Servicio | Permiso |
|----------|---------|
| IAM | `CreateRole`, `CreatePolicy`, `AttachRolePolicy`, `PassRole` |
| Lambda | `CreateFunction`, `UpdateFunctionCode`, `UpdateFunctionConfiguration` |
| EventBridge Scheduler | `CreateSchedule`, `CreateScheduleGroup` |
| CloudWatch Logs | `CreateLogGroup`, `PutRetentionPolicy` |
| CloudWatch Alarms | `PutMetricAlarm` |
| SNS | `CreateTopic`, `Subscribe` |
| Organizations | `ListAccounts`, `DescribeOrganization` |
| CloudFormation | `CreateStackSet`, `CreateStackInstances` (solo para StackSets) |

> Si tenés `AdministratorAccess` en la Management Account, cubrís todos estos permisos. Se puede acotar después.

---

## Checklist final

```
□  FASE 1: Rol IAM creado en todas las cuentas miembro (StackSets o Terraform)
□  FASE 1: Verificado que STS:AssumeRole funciona desde Management hacia miembro
□  FASE 2: Topic SNS creado y emails confirmados (si aplica)
□  FASE 3: Terraform aplicado en Management Account sin errores
□  FASE 4: Variables de entorno de la Lambda verificadas
□  FASE 5: Dry-run ejecutado y logs revisados sin errores de acceso
□  FASE 5: Instancias esperadas aparecen como "[DRY-RUN] Se apagaría..."
□  FASE 6: DRY_RUN seteado a false
□  FASE 6: Los 4 schedules de EventBridge en estado ENABLED
□  Tags environment aplicados en las instancias EC2 objetivo
□  Tag Referente aplicado en cada cuenta AWS (para notificaciones correctas)
```

---

## Troubleshooting rápido

| Error | Causa probable | Solución |
|-------|---------------|----------|
| `AccessDenied` al asumir rol | Trust policy incorrecta en cuenta miembro | Verificar `ManagementAccountId` en el StackSet (Fase 1) |
| `AccessDenied` al listar Organizations | La Lambda no tiene permisos | Verificar `management-account-policy.json` en el rol Lambda |
| No se encuentran instancias | Tag `environment` incorrecto | Verificar que el tag existe y tiene valor `sandbox`, `dev` o `desarrollo` |
| No llegan notificaciones SNS | Suscripción no confirmada | Buscar el email de confirmación de AWS SNS en bandeja de entrada |
| Lambda timeout | Muchas cuentas/regiones | Aumentar `lambda_timeout` a 600 en Terraform y re-aplicar |
| `external-id` no coincide | ExternalId incorrecto en trust policy | Verificar que dice `CCoE-EC2Scheduler` en la trust policy |
