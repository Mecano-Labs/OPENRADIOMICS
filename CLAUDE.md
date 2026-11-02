# RadMetrics (OpenRadiomics) - Claude Development Notes

**Target Domain**: radmetrics.ai

## Development Workflow

**We develop LOCALLY but against the CLOUD database (RDS), then deploy to App Runner daily.**

### Local Development
```bash
./run_local.sh
```
- Runs Flask on http://127.0.0.1:5000
- Connects to cloud RDS PostgreSQL database
- Uses cloud S3 bucket for DICOM storage
- Credentials stored in `.secrets.md` (gitignored)

### Deploy to Cloud (Daily)
```bash
aws codebuild start-build --project-name openradiomics-build --region us-east-1
```
App Runner auto-deploys when new image is pushed to ECR.

## Cloud Resources

| Resource | Value |
|----------|-------|
| **Target Domain** | radmetrics.ai (not yet configured) |
| **Live URL** | https://ezj2rdbghf.us-east-1.awsapprunner.com |
| **Region** | us-east-1 |
| **RDS Host** | openradiomics-dev-db.cwznsagquzwy.us-east-1.rds.amazonaws.com |
| **S3 Bucket** | openradiomics-dev-dicom-445305334312 |
| **ECR Repo** | 445305334312.dkr.ecr.us-east-1.amazonaws.com/openradiomics-dev |
| **Cognito User Pool ID** | us-east-1_SImbjXiLt |
| **Cognito Client ID** | 4pf34gh2u5sbd6l8hm54m0na9b |
| **Cognito Domain** | openradiomics-dev-445305334312.auth.us-east-1.amazoncognito.com |

## Credentials

**See `.secrets.md` for database password and connection strings.**

This file is gitignored and contains:
- RDS password
- Full connection strings
- Environment variable exports

## Architecture

- **Frontend**: Three.js-based MRI viewer (templates/viewer.html)
- **Backend**: Flask API (viewer_app.py)
- **Database**: PostgreSQL 15 on RDS (cloud, shared between local dev and production)
- **Storage**: S3 for DICOM files
- **Auth**: AWS Cognito (HIPAA-compliant with mandatory MFA)
- **Hosting**: AWS App Runner (auto-scaling container service)

## Key Files

| File | Purpose |
|------|---------|
| `viewer_app.py` | Flask backend API |
| `auth.py` | Authentication module (Cognito JWT validation) |
| `templates/viewer.html` | Main viewer UI |
| `templates/login.html` | Login page |
| `templates/register.html` | 3-step registration flow |
| `run_local.sh` | Local development script (gitignored) |
| `.secrets.md` | Credentials documentation (gitignored) |
| `infrastructure/terraform/*.tf` | AWS infrastructure |

## Registration Flow (3 Steps)

1. **Step 1**: Name, Email OR Phone, Consent → "Continue"
2. **Step 2**: Enter 6-digit verification code → "Verify"
3. **Step 3**: Set password → "Complete Registration"

Password is collected AFTER email verification, not before.

## Database

Using PostgreSQL on RDS. Schema in `infrastructure/terraform/schema.sql`.

**Tables**: patients, studies, series, instances, users, audit_log, annotations, measurements, etc.

To apply schema changes:
```python
import psycopg2
conn = psycopg2.connect(...)  # see .secrets.md
cur = conn.cursor()
cur.execute(open('infrastructure/terraform/schema.sql').read())
conn.commit()
```

## Environment Variables

### Local Development (set in run_local.sh)
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
- `S3_BUCKET_NAME`
- `AWS_REGION`

### Production (App Runner)
- `DATABASE_URL` - Full PostgreSQL connection string
- `COGNITO_USER_POOL_ID` - Cognito User Pool ID
- `COGNITO_CLIENT_ID` - Cognito App Client ID
- `COGNITO_DOMAIN` - Cognito hosted UI domain
- `SECRET_KEY` - Flask session secret
- `S3_BUCKET_NAME` - DICOM storage bucket

## Authentication (HIPAA Compliant)

- Cognito User Pool with mandatory MFA (NOT YET DEPLOYED)
- 15-minute access tokens, 8-hour refresh tokens
- Session timeout after 15 minutes of inactivity
- All access logged to `audit_log` table

## Infrastructure Changes

From `infrastructure/terraform/`:
```bash
terraform init
terraform plan
terraform apply
```

Remember to set `TF_VAR_db_password` before applying.

## Quick Commands

```bash
# Start local dev server
./run_local.sh

# Deploy to cloud
aws codebuild start-build --project-name openradiomics-build --region us-east-1

# Check cloud health
curl https://ezj2rdbghf.us-east-1.awsapprunner.com/api/health

# Check local health
curl http://127.0.0.1:5000/api/health

# Stop local server
pkill -f "python.*viewer_app"
```
