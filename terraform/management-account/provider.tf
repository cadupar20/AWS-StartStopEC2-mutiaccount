# ═══════════════════════════════════════════════════════════
# PROVIDER - MANAGEMENT ACCOUNT (Control Tower)
# ═══════════════════════════════════════════════════════════
#
# ⚠️  ADVERTENCIA CRÍTICA:
#    Este provider debe apuntar a la CUENTA MANAGEMENT de
#    AWS Control Tower. NO usar credenciales de cuentas miembro.
#
# ────────────────────────────────────────────────────────────
# ¿CÓMO CONFIGURAR?
# ────────────────────────────────────────────────────────────
#
# Opción A: Usar el perfil por defecto de AWS CLI
#   Asegurate de que tu sesión de AWS CLI apunte a la
#   cuenta Management:
#
#     aws sts get-caller-identity
#     {
#         "Account": "123456789012",  ← DEBE SER LA MANAGEMENT
#         "UserId": "...",
#         "Arn": "arn:aws:iam::123456789012:root"
#     }
#
# Opción B: Usar un perfil específico
#   Descomentar las líneas "profile" abajo y configurar
#   el perfil en ~/.aws/credentials:
#
#     [control-tower-admin]
#     aws_access_key_id = AKIA...
#     aws_secret_access_key = ...
#
# Opción C: Usar variables de entorno
#   export AWS_PROFILE=control-tower-admin
#   export AWS_REGION=us-east-1
#
# ═══════════════════════════════════════════════════════════

provider "aws" {
  # Región por defecto para los recursos
  region = "us-east-1"

  # ── Descomentar si usás un perfil específico ──
  # profile = "control-tower-admin"

  # ── Opcional: asumir un rol en la Management Account ──
  # assume_role {
  #   role_arn = "arn:aws:iam::MANAGEMENT_ACCOUNT_ID:role/AdminRole"
  # }
}