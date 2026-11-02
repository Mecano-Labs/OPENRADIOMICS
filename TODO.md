# TODO - Pre-Launch Checklist

## Database Migration (Required)
- [ ] Apply user_type column migration to RDS:
  ```sql
  ALTER TABLE users ADD COLUMN IF NOT EXISTS user_type VARCHAR(20) DEFAULT NULL;
  CREATE INDEX IF NOT EXISTS idx_users_user_type ON users(user_type);
  INSERT INTO schema_version (version, description)
    VALUES (5, 'Add user_type column for patient/provider classification')
    ON CONFLICT (version) DO NOTHING;
  ```

## Domain & Email Setup
- [ ] Purchase `radmetrics.ai` domain
- [ ] Configure DNS for radmetrics.ai
- [ ] Add domain identity in SES (Create identity → radmetrics.ai)
- [ ] Add DKIM/SPF DNS records for SES verification
- [ ] Request SES production access (after domain verified)
- [ ] Update `auth.py` line 936: change sender from `samir8787@gmail.com` to `noreply@radmetrics.ai`

## Authentication
- [ ] Deploy Cognito User Pool (currently marked as NOT YET DEPLOYED)
- [ ] Configure App Runner environment variables for Cognito

## Infrastructure
- [ ] Configure custom domain in App Runner (radmetrics.ai → ezj2rdbghf.us-east-1.awsapprunner.com)
- [ ] Set up SSL certificate for radmetrics.ai

## Current Limitations (Sandbox Mode)
- SES only sends to verified email addresses
- Testing: use `samir8787@gmail.com` for registration
