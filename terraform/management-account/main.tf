# ═══════════════════════════════════════════════════════════
# CCoE EC2 Scheduler - MANAGEMENT ACCOUNT (Control Tower)
# ═══════════════════════════════════════════════════════════
#
# ⚠️  IMPORTANTE: Este Terraform se ejecuta EXCLUSIVAMENTE en la
#    cuenta MANAGEMENT de AWS Control Tower (también llamada
#    "Management Account" o "Payer Account").
#
#    NO ejecutar esto en cuentas miembro.
#
# ────────────────────────────────────────────────────────────
# ¿QUÉ HACE ESTE TERRAFORM?
# ────────────────────────────────────────────────────────────
#
# En la cuenta MANAGEMENT de Control Tower, crea:
#
#  1. IAM Role  → CCoE-EC2Scheduler-Management-Role
#     (rol que usará la Lambda para ejecutarse)
#
#  2. IAM Policy → CCoE-EC2Scheduler-Management-Policy
#     (permisos: Organizations:ListAccounts, STS:AssumeRole,
#      SNS:Publish, CloudWatch Logs)
#
#  3. Lambda Function → CCoE-EC2Scheduler-CrossAccount
#     (Python 3.12 - el código que apaga/enciende EC2)
#
#  4. IAM Role → CCoE-EC2Scheduler-EventBridge-Role
#     (rol para que EventBridge pueda invocar la Lambda)
#
#  5. EventBridge Scheduler → 4 schedules (stop/start × dev/sandbox)
#     (disparan la Lambda en horarios programados)
#
#  6. CloudWatch Log Group → /aws/lambda/CCoE-EC2Scheduler-CrossAccount
#     (logs de la Lambda con retención de 30 días)
#
#  7. CloudWatch Alarm → CCoE-EC2Scheduler-Errors
#     (alerta si la Lambda falla)
#
# ────────────────────────────────────────────────────────────
# ¿CÓMO FUNCIONA EL CROSS-ACCOUNT?
# ────────────────────────────────────────────────────────────
#
#  La Lambda EJECUTA en la cuenta Management y:
#  1. Llama a organizations:ListAccounts → descubre TODAS las cuentas
#  2. Por cada cuenta, ejecuta STS:AssumeRole hacia:
#       arn:aws:iam::<CUENTA_MIEMBRO>:role/CCoE-EC2Scheduler-Role
#  3. Con esas credenciales temporales, opera EC2 en cada cuenta
#
#  PREREQUISITO: El rol "CCoE-EC2Scheduler-Role" debe existir en
#  cada cuenta miembro (ver cloudformation/member-account-role.yaml
#  para StackSets)
#
# ═══════════════════════════════════════════════════════════

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# ──────────────────────────────────────────────
# VARIABLES DE CONFIGURACIÓN
# ──────────────────────────────────────────────

variable "environments" {
  description = "Ambientes a gestionar (separados por coma). Ej: sandbox,dev,desarrollo"
  type        = string
  default     = "sandbox,dev,desarrollo"
}

variable "regions" {
  description = "Regiones AWS donde buscar EC2. Usar 'all' para descubrir automáticamente"
  type        = string
  default     = "us-east-1,us-east-2,us-west-2,sa-east-1"
}

variable "excluded_accounts" {
  description = "IDs de cuenta a EXCLUIR (separados por coma). Útil para excluir la Management Account"
  type        = string
  default     = ""
}

variable "assume_role_name" {
  description = "Nombre del rol IAM que la Lambda asumirá en cada cuenta miembro"
  type        = string
  default     = "CCoE-EC2Scheduler-Role"
}

variable "sns_topic_arn" {
  description = "ARN del topic SNS para notificaciones. Dejar vacío para deshabilitar SNS"
  type        = string
  default     = ""
}

variable "sns_fallback_email" {
  description = "Email de fallback para notificaciones SNS (cuando la cuenta no tiene tag Referente)"
  type        = string
  default     = "ccoe-team@empresa.com"
}

variable "lambda_timeout" {
  description = "Timeout de la Lambda en segundos (máx 900)"
  type        = number
  default     = 300
}

variable "lambda_memory_size" {
  description = "Memoria de la Lambda en MB (mín 128)"
  type        = number
  default     = 256
}

variable "log_retention_days" {
  description = "Días de retención de logs en CloudWatch"
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags a aplicar a todos los recursos"
  type        = map(string)
  default = {
    Name        = "CCoE-EC2Scheduler"
    team        = "ccoe"
    environment = "management"
    managed-by  = "terraform"
  }
}

# ──────────────────────────────────────────────
# 1. IAM ROLE PARA LA LAMBDA
# ──────────────────────────────────────────────
# Este rol lo asume la función Lambda para ejecutarse.
# Tiene permisos para:
#   - organizations:ListAccounts (descubrir cuentas)
#   - sts:AssumeRole (asumir rol en cada cuenta miembro)
#   - sns:Publish (enviar notificaciones)
#   - logs:* (escribir logs en CloudWatch)

resource "aws_iam_role" "lambda_role" {
  name = "CCoE-EC2Scheduler-Management-Role"

  description = "Rol que usa la Lambda CCoE-EC2Scheduler-CrossAccount para ejecutarse en la cuenta Management de Control Tower"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = var.tags
}

# ──────────────────────────────────────────────
# 2. IAM POLICY PARA LA LAMBDA
# ──────────────────────────────────────────────

resource "aws_iam_policy" "lambda_policy" {
  name        = "CCoE-EC2Scheduler-Management-Policy"
  description = "Permisos para que la Lambda descubra cuentas (Organizations), asuma roles (STS), publique notificaciones (SNS) y registre logs (CloudWatch)"
  policy      = file("${path.module}/../../iam/management-account-policy.json")

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "lambda_policy_attach" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = aws_iam_policy.lambda_policy.arn
}

# ──────────────────────────────────────────────
# 3. IAM ROLE PARA EVENTBRIDGE SCHEDULER
# ──────────────────────────────────────────────
# Este rol lo usa EventBridge Scheduler para invocar
# la Lambda en los horarios programados.

resource "aws_iam_role" "scheduler_role" {
  name = "CCoE-EC2Scheduler-EventBridge-Role"

  description = "Rol que usa EventBridge Scheduler para invocar la Lambda CCoE-EC2Scheduler-CrossAccount"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "scheduler.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "scheduler_policy" {
  name = "CCoE-EC2Scheduler-EventBridge-Policy"
  role = aws_iam_role.scheduler_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = [aws_lambda_function.scheduler.arn]
      }
    ]
  })
}

# ──────────────────────────────────────────────
# 4. LAMBDA FUNCTION
# ──────────────────────────────────────────────
# Runtime: Python 3.12
# Handler: ec2_scheduler_cross_account.lambda_handler
#
# La Lambda EJECUTA en la Management Account de Control Tower
# y descubre todas las cuentas via Organizations.
# Luego asume el rol CCoE-EC2Scheduler-Role en cada cuenta.

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/../../src/ec2_scheduler_cross_account.py"
  output_path = "${path.module}/ec2_scheduler_cross_account.zip"
}

resource "aws_lambda_function" "scheduler" {
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  function_name    = "CCoE-EC2Scheduler-CrossAccount"
  role             = aws_iam_role.lambda_role.arn
  handler          = "ec2_scheduler_cross_account.lambda_handler"
  runtime          = "python3.12"
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory_size

  environment {
    variables = {
      # Ambientes: separados por coma
      ENVIRONMENTS = var.environments
      # Regiones: separadas por coma, o 'all'
      REGIONS = var.regions
      # Cuentas a excluir (ej: la management)
      EXCLUDED_ACCOUNTS = var.excluded_accounts
      # Nombre del rol a asumir en cada cuenta miembro
      ASSUME_ROLE_NAME = var.assume_role_name
      # ARN del topic SNS (vacio = sin notificaciones)
      SNS_TOPIC_ARN = var.sns_topic_arn
      # Email de fallback para SNS (cuando la cuenta no tiene tag Referente)
      SNS_FALLBACK_EMAIL = var.sns_fallback_email
    }
  }

  tags = var.tags
}

# ──────────────────────────────────────────────
# 5. EVENTBRIDGE SCHEDULER - SCHEDULES
# ──────────────────────────────────────────────
# Cada schedule dispara la Lambda con un payload JSON
# que indica la acción a realizar (stop o start).
#
# La Lambda internamente descubre las cuentas via
# Organizations y procesa cada una.
#
# IMPORTANTE: Los horarios están en ART (UTC-3)
# usando schedule_expression_timezone.

# ── Apagado Desarrollo: Lun-Vie 20:00 ART ──
resource "aws_scheduler_schedule" "stop_dev" {
  name                         = "CCoE-Stop-Desarrollo"
  group_name                   = "default"
  schedule_expression          = "cron(0 23 ? * MON-FRI *)"
  schedule_expression_timezone = "America/Argentina/Buenos_Aires"
  state                        = "ENABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.scheduler.arn
    role_arn = aws_iam_role.scheduler_role.arn
    input = jsonencode({
      action = "stop"
      # Nota: no se especifican cuentas ni regiones aquí.
      # La Lambda descubre TODO automáticamente.
    })
  }

  depends_on = [aws_lambda_function.scheduler]
}

# ── Apagado Sandbox: Lun-Vie 19:00 ART ──
resource "aws_scheduler_schedule" "stop_sandbox" {
  name                         = "CCoE-Stop-Sandbox"
  group_name                   = "default"
  schedule_expression          = "cron(0 22 ? * MON-FRI *)"
  schedule_expression_timezone = "America/Argentina/Buenos_Aires"
  state                        = "ENABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.scheduler.arn
    role_arn = aws_iam_role.scheduler_role.arn
    input = jsonencode({
      action = "stop"
    })
  }

  depends_on = [aws_lambda_function.scheduler]
}

# ── Encendido Desarrollo: Lun-Vie 08:00 ART ──
resource "aws_scheduler_schedule" "start_dev" {
  name                         = "CCoE-Start-Desarrollo"
  group_name                   = "default"
  schedule_expression          = "cron(0 11 ? * MON-FRI *)"
  schedule_expression_timezone = "America/Argentina/Buenos_Aires"
  state                        = "ENABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.scheduler.arn
    role_arn = aws_iam_role.scheduler_role.arn
    input = jsonencode({
      action = "start"
    })
  }

  depends_on = [aws_lambda_function.scheduler]
}

# ── Encendido Sandbox: Lun-Vie 09:00 ART ──
resource "aws_scheduler_schedule" "start_sandbox" {
  name                         = "CCoE-Start-Sandbox"
  group_name                   = "default"
  schedule_expression          = "cron(0 12 ? * MON-FRI *)"
  schedule_expression_timezone = "America/Argentina/Buenos_Aires"
  state                        = "ENABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.scheduler.arn
    role_arn = aws_iam_role.scheduler_role.arn
    input = jsonencode({
      action = "start"
    })
  }

  depends_on = [aws_lambda_function.scheduler]
}

# ──────────────────────────────────────────────
# 6. CLOUDWATCH LOG GROUP
# ──────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "lambda_logs" {
  name              = "/aws/lambda/CCoE-EC2Scheduler-CrossAccount"
  retention_in_days = var.log_retention_days

  tags = var.tags
}

# ──────────────────────────────────────────────
# 7. CLOUDWATCH ALARM (monitoreo)
# ──────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "CCoE-EC2Scheduler-Errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "1"
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = "300"
  statistic           = "Sum"
  threshold           = "0"
  alarm_description   = "⚠️ Alerta: La Lambda CCoE-EC2Scheduler-CrossAccount tuvo errores en su última ejecución"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.scheduler.function_name
  }

  tags = var.tags
}

# ──────────────────────────────────────────────
# OUTPUTS
# ──────────────────────────────────────────────

output "lambda_function_name" {
  description = "Nombre de la Lambda creada en la Management Account"
  value       = aws_lambda_function.scheduler.function_name
}

output "lambda_function_arn" {
  description = "ARN completo de la Lambda"
  value       = aws_lambda_function.scheduler.arn
}

output "lambda_role_arn" {
  description = "ARN del rol IAM que usa la Lambda (CCoE-EC2Scheduler-Management-Role)"
  value       = aws_iam_role.lambda_role.arn
}

output "schedules_created" {
  description = "Lista de los 4 schedules de EventBridge creados"
  value = [
    "${aws_scheduler_schedule.stop_dev.name} (Stop Desarrollo - 20:00 ART)",
    "${aws_scheduler_schedule.stop_sandbox.name} (Stop Sandbox - 19:00 ART)",
    "${aws_scheduler_schedule.start_dev.name} (Start Desarrollo - 08:00 ART)",
    "${aws_scheduler_schedule.start_sandbox.name} (Start Sandbox - 09:00 ART)"
  ]
}