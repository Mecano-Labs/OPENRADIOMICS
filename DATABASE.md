# OpenRadiomics Database Schema

## Overview

OpenRadiomics uses **RDS PostgreSQL** as its primary database, storing DICOM metadata in a normalized schema. DICOM files are stored in S3, with the database containing `s3_key` references to the actual files.

---

## Schema Summary

| Category | Tables | Description |
|----------|--------|-------------|
| **DICOM Hierarchy** | `patients`, `studies`, `series`, `instances` | Normalized DICOM metadata following the Patient → Study → Series → Instance hierarchy |
| **Users & Auth** | `users`, `pending_registrations`, `password_reset_tokens` | Application users, registration flow, password reset |
| **Access Control** | `study_access`, `patient_access`, `share_links` | Row-level access control and sharing |
| **Features** | `favorites`, `annotations`, `measurements`, `analysis_results` | User annotations, measurements, and AI outputs |
| **Audit** | `audit_log` | HIPAA-compliant access logging |

---

## Entity Relationship Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              DICOM HIERARCHY                                         │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                      │
│  ┌─────────────┐       ┌─────────────┐       ┌─────────────┐       ┌─────────────┐  │
│  │  patients   │       │   studies   │       │   series    │       │  instances  │  │
│  ├─────────────┤       ├─────────────┤       ├─────────────┤       ├─────────────┤  │
│  │ id (PK)     │◀──┐   │ id (PK)     │◀──┐   │ id (PK)     │◀──┐   │ id (PK)     │  │
│  │ patient_uid │   │   │ patient_id  │───┘   │ study_id    │───┘   │ series_id   │──┘
│  │ name        │   │   │ study_uid   │       │ series_uid  │       │ sop_uid     │
│  │ birth_date  │   │   │ description │       │ description │       │ instance_num│
│  │ sex         │   │   │ study_date  │       │ modality    │       │ s3_key      │
│  │ weight      │   │   │ accession   │       │ mri_params  │       │ slice_loc   │
│  └─────────────┘   │   └─────────────┘       │ geometry    │       │ metadata    │
│                    │                          └─────────────┘       └─────────────┘
│                    │                                                                 │
└────────────────────┼─────────────────────────────────────────────────────────────────┘
                     │
┌────────────────────┼─────────────────────────────────────────────────────────────────┐
│                    │         USERS & ACCESS CONTROL                                  │
├────────────────────┼─────────────────────────────────────────────────────────────────┤
│                    │                                                                 │
│  ┌─────────────┐   │   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐       │
│  │   users     │   │   │ study_access │   │patient_access│   │ share_links  │       │
│  ├─────────────┤   │   ├──────────────┤   ├──────────────┤   ├──────────────┤       │
│  │ id (PK)     │◀──┼───│ user_id      │   │ user_id      │   │ id (PK)      │       │
│  │ cognito_sub │   │   │ study_id ────│───│ patient_id ──│───│ study_id     │       │
│  │ email       │   │   │ access_level │   │ access_level │   │ token        │       │
│  │ display_name│   │   │ is_owner     │   │ granted_by   │   │ expires_at   │       │
│  │ role        │   │   │ granted_at   │   │ granted_at   │   │ created_by   │       │
│  └─────────────┘   │   └──────────────┘   └──────────────┘   └──────────────┘       │
│                    │                                                                 │
└────────────────────┼─────────────────────────────────────────────────────────────────┘
                     │
┌────────────────────┼─────────────────────────────────────────────────────────────────┐
│                    │                    FEATURES                                     │
├────────────────────┼─────────────────────────────────────────────────────────────────┤
│                    │                                                                 │
│  ┌─────────────┐   │   ┌─────────────┐   ┌─────────────┐   ┌────────────────┐       │
│  │  favorites  │   │   │ annotations │   │measurements │   │analysis_results│       │
│  ├─────────────┤   │   ├─────────────┤   ├─────────────┤   ├────────────────┤       │
│  │ id (PK)     │   │   │ id (PK)     │   │ id (PK)     │   │ id (PK)        │       │
│  │ user_id ────│───┘   │ user_id     │   │ user_id     │   │ series_id      │       │
│  │ study_id    │       │ instance_id │   │ series_id   │   │ analysis_type  │       │
│  │ series_id   │       │ series_id   │   │ type        │   │ model_version  │       │
│  │ notes       │       │ type        │   │ coordinates │   │ results        │       │
│  │ created_at  │       │ coordinates │   │ value, unit │   │ confidence     │       │
│  └─────────────┘       │ label, color│   └─────────────┘   └────────────────┘       │
│                        └─────────────┘                                               │
│                                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

---

## DICOM Hierarchy Tables

### `patients`

Patient demographics from DICOM Patient-level tags.

```sql
CREATE TABLE patients (
    id              SERIAL PRIMARY KEY,
    patient_uid     VARCHAR(255) UNIQUE NOT NULL,   -- PatientID from DICOM
    name            VARCHAR(255),                    -- PatientName
    birth_date      DATE,                            -- PatientBirthDate
    sex             CHAR(1),                         -- PatientSex: M/F/O
    weight          NUMERIC(6,2),                    -- PatientWeight in kg
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_patients_uid ON patients(patient_uid);
CREATE INDEX idx_patients_name ON patients(name);
```

| Column | Type | Source DICOM Tag | Description |
|--------|------|------------------|-------------|
| `id` | SERIAL | - | Internal primary key |
| `patient_uid` | VARCHAR(255) | PatientID (0010,0020) | DICOM patient identifier (UNIQUE) |
| `name` | VARCHAR(255) | PatientName (0010,0010) | Patient name |
| `birth_date` | DATE | PatientBirthDate (0010,0030) | Date of birth |
| `sex` | CHAR(1) | PatientSex (0010,0040) | M=Male, F=Female, O=Other |
| `weight` | NUMERIC(6,2) | PatientWeight (0010,1030) | Weight in kilograms |

---

### `studies`

Study-level information. One patient can have multiple studies.

```sql
CREATE TABLE studies (
    id              SERIAL PRIMARY KEY,
    patient_id      INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    study_uid       VARCHAR(255) UNIQUE NOT NULL,   -- StudyInstanceUID
    description     VARCHAR(500),                    -- StudyDescription
    study_date      DATE,                            -- StudyDate
    study_time      TIME,                            -- StudyTime
    accession       VARCHAR(100),                    -- AccessionNumber
    institution     VARCHAR(255),                    -- InstitutionName
    referring_physician VARCHAR(255),                -- ReferringPhysicianName
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_studies_patient ON studies(patient_id);
CREATE INDEX idx_studies_uid ON studies(study_uid);
CREATE INDEX idx_studies_date ON studies(study_date DESC);
CREATE INDEX idx_studies_accession ON studies(accession);
```

| Column | Type | Source DICOM Tag | Description |
|--------|------|------------------|-------------|
| `id` | SERIAL | - | Internal primary key |
| `patient_id` | INTEGER | - | Foreign key to patients |
| `study_uid` | VARCHAR(255) | StudyInstanceUID (0020,000D) | DICOM study identifier (UNIQUE) |
| `description` | VARCHAR(500) | StudyDescription (0008,1030) | e.g., "MRI BRAIN W WO CONTRAST" |
| `study_date` | DATE | StudyDate (0008,0020) | Date of study |
| `study_time` | TIME | StudyTime (0008,0030) | Time of study |
| `accession` | VARCHAR(100) | AccessionNumber (0008,0050) | Hospital accession number |
| `institution` | VARCHAR(255) | InstitutionName (0008,0080) | Hospital/facility name |
| `referring_physician` | VARCHAR(255) | ReferringPhysicianName (0008,0090) | Ordering physician |

---

### `series`

Series/sequence-level information with MRI parameters. One study can have multiple series.

```sql
CREATE TABLE series (
    id                  SERIAL PRIMARY KEY,
    study_id            INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    series_uid          VARCHAR(255) UNIQUE NOT NULL,   -- SeriesInstanceUID
    description         VARCHAR(500),                    -- SeriesDescription
    series_number       INTEGER,                         -- SeriesNumber
    modality            VARCHAR(10) NOT NULL,            -- Modality (MR, CT, etc.)
    body_part           VARCHAR(100),                    -- BodyPartExamined

    -- MRI Parameters (JSONB for flexibility)
    mri_params          JSONB,
    -- Contains: {
    --   "magnetic_field_strength": 3.0,      -- Tesla
    --   "repetition_time": 2000.0,           -- TR in ms
    --   "echo_time": 80.0,                   -- TE in ms
    --   "inversion_time": 2500.0,            -- TI in ms
    --   "flip_angle": 90.0,                  -- degrees
    --   "echo_train_length": 16,
    --   "number_of_averages": 2.0,           -- NEX/NSA
    --   "scanning_sequence": "SE",
    --   "sequence_variant": "SK",
    --   "sequence_name": "tse2d1_15",
    --   "mr_acquisition_type": "2D"
    -- }

    -- Equipment
    manufacturer        VARCHAR(255),                    -- Manufacturer
    scanner_model       VARCHAR(255),                    -- ManufacturerModelName
    station_name        VARCHAR(255),                    -- StationName
    software_versions   VARCHAR(255),                    -- SoftwareVersions

    -- Geometry (consistent across series)
    slice_thickness     NUMERIC(10,4),                   -- SliceThickness in mm
    spacing_between     NUMERIC(10,4),                   -- SpacingBetweenSlices in mm
    pixel_spacing       NUMERIC(10,4)[2],                -- PixelSpacing [row, col] in mm

    -- Contrast
    contrast_agent      VARCHAR(255),                    -- ContrastBolusAgent

    -- Counts
    instance_count      INTEGER DEFAULT 0,               -- Number of slices

    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_series_study ON series(study_id);
CREATE INDEX idx_series_uid ON series(series_uid);
CREATE INDEX idx_series_modality ON series(modality);
CREATE INDEX idx_series_description ON series(description);
```

| Column | Type | Source DICOM Tag | Description |
|--------|------|------------------|-------------|
| `id` | SERIAL | - | Internal primary key |
| `study_id` | INTEGER | - | Foreign key to studies |
| `series_uid` | VARCHAR(255) | SeriesInstanceUID (0020,000E) | DICOM series identifier (UNIQUE) |
| `description` | VARCHAR(500) | SeriesDescription (0008,103E) | e.g., "AX T2 FLAIR", "SAG T1 MPRAGE" |
| `series_number` | INTEGER | SeriesNumber (0020,0011) | Series number within study |
| `modality` | VARCHAR(10) | Modality (0008,0060) | MR, CT, PT, US, etc. |
| `body_part` | VARCHAR(100) | BodyPartExamined (0018,0015) | e.g., "BRAIN", "SPINE" |
| `mri_params` | JSONB | Various (0018,xxxx) | MRI-specific parameters |
| `manufacturer` | VARCHAR(255) | Manufacturer (0008,0070) | Scanner manufacturer |
| `scanner_model` | VARCHAR(255) | ManufacturerModelName (0008,1090) | Scanner model |
| `slice_thickness` | NUMERIC(10,4) | SliceThickness (0018,0050) | Slice thickness in mm |
| `pixel_spacing` | NUMERIC(10,4)[2] | PixelSpacing (0028,0030) | [row, col] pixel size in mm |

#### MRI Parameters JSONB Structure

| Key | Source DICOM Tag | Description |
|-----|------------------|-------------|
| `magnetic_field_strength` | MagneticFieldStrength (0018,0087) | Field strength in Tesla |
| `repetition_time` | RepetitionTime (0018,0080) | TR in milliseconds |
| `echo_time` | EchoTime (0018,0081) | TE in milliseconds |
| `inversion_time` | InversionTime (0018,0082) | TI in milliseconds |
| `flip_angle` | FlipAngle (0018,1314) | Flip angle in degrees |
| `echo_train_length` | EchoTrainLength (0018,0091) | Number of echoes |
| `number_of_averages` | NumberOfAverages (0018,0083) | NEX/NSA |
| `scanning_sequence` | ScanningSequence (0018,0020) | SE, GR, IR, EP, etc. |
| `sequence_variant` | SequenceVariant (0018,0021) | SK, MTC, SS, etc. |
| `sequence_name` | SequenceName (0018,0024) | Manufacturer sequence name |
| `mr_acquisition_type` | MRAcquisitionType (0018,0023) | 2D or 3D |
| `imaging_frequency` | ImagingFrequency (0018,0084) | Larmor frequency in MHz |

---

### `instances`

Individual DICOM images (slices). One series has multiple instances.

```sql
CREATE TABLE instances (
    id                  SERIAL PRIMARY KEY,
    series_id           INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    sop_uid             VARCHAR(255) UNIQUE NOT NULL,   -- SOPInstanceUID
    instance_number     INTEGER,                         -- InstanceNumber
    s3_key              VARCHAR(500) NOT NULL,           -- S3 object key (replaces file_path)

    -- Spatial Position
    slice_location      NUMERIC(12,4),                   -- SliceLocation in mm
    image_position      NUMERIC(12,4)[3],                -- ImagePositionPatient [x, y, z]
    image_orientation   NUMERIC(12,6)[6],                -- ImageOrientationPatient [6 cosines]

    -- Image Dimensions
    rows                INTEGER,                          -- Rows
    columns             INTEGER,                          -- Columns

    -- Pixel Data Properties
    bits_allocated      SMALLINT,                         -- BitsAllocated
    bits_stored         SMALLINT,                         -- BitsStored
    high_bit            SMALLINT,                         -- HighBit
    pixel_representation SMALLINT,                        -- 0=unsigned, 1=signed
    rescale_intercept   NUMERIC(10,4),                    -- RescaleIntercept
    rescale_slope       NUMERIC(10,4),                    -- RescaleSlope

    -- Display Defaults
    window_center       NUMERIC(10,2),                    -- WindowCenter
    window_width        NUMERIC(10,2),                    -- WindowWidth

    -- Timestamps
    acquisition_datetime TIMESTAMPTZ,                     -- AcquisitionDateTime
    content_datetime    TIMESTAMPTZ,                      -- ContentDate + ContentTime

    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_instances_series ON instances(series_id);
CREATE INDEX idx_instances_uid ON instances(sop_uid);
CREATE INDEX idx_instances_number ON instances(series_id, instance_number);
CREATE INDEX idx_instances_slice ON instances(series_id, slice_location);
CREATE INDEX idx_instances_s3 ON instances(s3_key);
```

| Column | Type | Source DICOM Tag | Description |
|--------|------|------------------|-------------|
| `id` | SERIAL | - | Internal primary key |
| `series_id` | INTEGER | - | Foreign key to series |
| `sop_uid` | VARCHAR(255) | SOPInstanceUID (0008,0018) | DICOM instance identifier (UNIQUE) |
| `instance_number` | INTEGER | InstanceNumber (0020,0013) | Slice number within series |
| `s3_key` | VARCHAR(500) | - | S3 object key for DICOM file |
| `slice_location` | NUMERIC(12,4) | SliceLocation (0020,1041) | Position along scan axis in mm |
| `image_position` | NUMERIC(12,4)[3] | ImagePositionPatient (0020,0032) | 3D position [x, y, z] in mm |
| `image_orientation` | NUMERIC(12,6)[6] | ImageOrientationPatient (0020,0037) | Row/column direction cosines |
| `rows` | INTEGER | Rows (0028,0010) | Image height in pixels |
| `columns` | INTEGER | Columns (0028,0011) | Image width in pixels |
| `window_center` | NUMERIC(10,2) | WindowCenter (0028,1050) | Default window center |
| `window_width` | NUMERIC(10,2) | WindowWidth (0028,1051) | Default window width |

---

## User & Authentication Tables

### `users`

Application users linked to AWS Cognito for authentication.

```sql
CREATE TABLE users (
    id              SERIAL PRIMARY KEY,
    cognito_sub     VARCHAR(255) UNIQUE NOT NULL,   -- Cognito user ID (sub claim)
    email           VARCHAR(255) UNIQUE NOT NULL,
    display_name    VARCHAR(255),
    role            VARCHAR(50) DEFAULT 'viewer',   -- 'admin', 'clinician', 'researcher', 'viewer'
    preferences     JSONB DEFAULT '{}',             -- User preferences (theme, defaults, etc.)
    last_login      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_users_cognito ON users(cognito_sub);
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_role ON users(role);
```

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL | Internal primary key |
| `cognito_sub` | VARCHAR(255) | Cognito user ID (from JWT `sub` claim) |
| `email` | VARCHAR(255) | User email address |
| `display_name` | VARCHAR(255) | Display name for UI |
| `role` | VARCHAR(50) | Permission level: admin, clinician, researcher, viewer |
| `preferences` | JSONB | User settings (theme, window presets, etc.) |

#### User Roles

| Role | Description | Permissions |
|------|-------------|-------------|
| `admin` | System administrator | Full access, user management |
| `clinician` | Medical professional | View, annotate, share studies |
| `researcher` | Research user | View, annotate, export data |
| `viewer` | Read-only access | View only |

---

### `pending_registrations`

Tracks in-progress user registrations with verification codes and rate limiting.

```sql
CREATE TABLE pending_registrations (
    id                      SERIAL PRIMARY KEY,
    email                   VARCHAR(255),
    phone                   VARCHAR(20),
    first_name              VARCHAR(100),
    last_name               VARCHAR(100),
    verification_code       VARCHAR(10),
    code_expires_at         TIMESTAMPTZ NOT NULL,
    last_email_sent_at      TIMESTAMPTZ,
    last_sms_sent_at        TIMESTAMPTZ,
    email_send_count        INTEGER DEFAULT 0,
    sms_send_count          INTEGER DEFAULT 0,
    status                  VARCHAR(20) DEFAULT 'pending',  -- 'pending', 'verified', 'completed', 'expired'
    form_data               JSONB,                          -- Additional registration form data
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(email),
    UNIQUE(phone)
);

CREATE INDEX idx_pending_reg_email ON pending_registrations(email);
CREATE INDEX idx_pending_reg_phone ON pending_registrations(phone);
CREATE INDEX idx_pending_reg_status ON pending_registrations(status);
```

| Column | Type | Description |
|--------|------|-------------|
| `email` | VARCHAR(255) | Email address (unique) |
| `phone` | VARCHAR(20) | Phone number in E.164 format (unique) |
| `verification_code` | VARCHAR(10) | 6-digit verification code |
| `code_expires_at` | TIMESTAMPTZ | When the code expires (10 minutes) |
| `email_send_count` | INTEGER | Rate limiting: times email sent |
| `sms_send_count` | INTEGER | Rate limiting: times SMS sent |
| `status` | VARCHAR(20) | pending → verified → completed |
| `form_data` | JSONB | Additional form fields (address, consent, etc.) |

---

### `password_reset_tokens`

Secure tokens for link-based password reset flow.

```sql
CREATE TABLE password_reset_tokens (
    id              SERIAL PRIMARY KEY,
    email           VARCHAR(255) NOT NULL,
    token           VARCHAR(64) UNIQUE NOT NULL,    -- Secure random token (URL-safe)
    expires_at      TIMESTAMPTZ NOT NULL,           -- 1 hour expiry
    used_at         TIMESTAMPTZ,                    -- When token was used (NULL if unused)
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_password_reset_token ON password_reset_tokens(token);
CREATE INDEX idx_password_reset_email ON password_reset_tokens(email);
CREATE INDEX idx_password_reset_expires ON password_reset_tokens(expires_at);
```

| Column | Type | Description |
|--------|------|-------------|
| `email` | VARCHAR(255) | User's email address |
| `token` | VARCHAR(64) | Cryptographically secure random token |
| `expires_at` | TIMESTAMPTZ | Token expiration (1 hour from creation) |
| `used_at` | TIMESTAMPTZ | When token was used (prevents reuse) |

**Flow:**
1. User requests password reset → token created, email sent with link
2. User clicks link (`/auth/reset-password?token=xxx`) → token validated
3. User sets new password → token marked as used

---

## Access Control Tables

### `study_access`

Primary sharing mechanism: controls which users can access which studies.

```sql
CREATE TABLE study_access (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    study_id        INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    access_level    VARCHAR(50) DEFAULT 'view',     -- 'view', 'annotate', 'admin'
    is_owner        BOOLEAN DEFAULT FALSE,          -- Did this user upload it?
    granted_by      INTEGER REFERENCES users(id),
    granted_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,                    -- Optional expiration
    UNIQUE(user_id, study_id)
);

CREATE INDEX idx_study_access_user ON study_access(user_id);
CREATE INDEX idx_study_access_study ON study_access(study_id);
CREATE INDEX idx_study_access_expires ON study_access(expires_at) WHERE expires_at IS NOT NULL;
```

| Column | Type | Description |
|--------|------|-------------|
| `user_id` | INTEGER | User being granted access |
| `study_id` | INTEGER | Study being shared |
| `access_level` | VARCHAR(50) | view, annotate, or admin |
| `is_owner` | BOOLEAN | True if user uploaded this study |
| `granted_by` | INTEGER | User who granted access |
| `expires_at` | TIMESTAMPTZ | Optional expiration date |

---

### `patient_access`

Optional: Grant access to ALL studies for a patient (useful for longitudinal care).

```sql
CREATE TABLE patient_access (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    patient_id      INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    access_level    VARCHAR(50) DEFAULT 'view',
    granted_by      INTEGER REFERENCES users(id),
    granted_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    UNIQUE(user_id, patient_id)
);

CREATE INDEX idx_patient_access_user ON patient_access(user_id);
CREATE INDEX idx_patient_access_patient ON patient_access(patient_id);
```

---

### `share_links`

Anonymous share links with expiration for external sharing.

```sql
CREATE TABLE share_links (
    id              SERIAL PRIMARY KEY,
    study_id        INTEGER NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    token           VARCHAR(64) UNIQUE NOT NULL,    -- Random URL-safe token
    access_level    VARCHAR(50) DEFAULT 'view',
    created_by      INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    max_uses        INTEGER,                        -- Optional use limit
    use_count       INTEGER DEFAULT 0,
    is_active       BOOLEAN DEFAULT TRUE
);

CREATE INDEX idx_share_links_token ON share_links(token);
CREATE INDEX idx_share_links_study ON share_links(study_id);
CREATE INDEX idx_share_links_expires ON share_links(expires_at);
```

---

## Feature Tables

### `favorites`

User bookmarks for quick access to studies or series.

```sql
CREATE TABLE favorites (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    study_id        INTEGER REFERENCES studies(id) ON DELETE CASCADE,
    series_id       INTEGER REFERENCES series(id) ON DELETE CASCADE,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT favorites_target CHECK (study_id IS NOT NULL OR series_id IS NOT NULL)
);

CREATE INDEX idx_favorites_user ON favorites(user_id);
CREATE INDEX idx_favorites_study ON favorites(study_id) WHERE study_id IS NOT NULL;
CREATE INDEX idx_favorites_series ON favorites(series_id) WHERE series_id IS NOT NULL;
```

---

### `annotations`

User annotations on images: points, lines, polygons, freehand drawings.

```sql
CREATE TABLE annotations (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Target (instance for 2D, series for 3D spanning multiple slices)
    instance_id         INTEGER REFERENCES instances(id) ON DELETE CASCADE,
    series_id           INTEGER REFERENCES series(id) ON DELETE CASCADE,

    annotation_type     VARCHAR(50) NOT NULL,       -- 'point', 'line', 'polygon', 'freehand', 'ellipse', 'rectangle'
    coordinates         JSONB NOT NULL,             -- Shape-specific coordinate data
    -- Point:    {"x": 100, "y": 200, "z": 50}
    -- Line:     {"points": [{"x": 0, "y": 0}, {"x": 100, "y": 100}]}
    -- Polygon:  {"points": [{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 50, "y": 100}]}
    -- Ellipse:  {"center": {"x": 50, "y": 50}, "rx": 30, "ry": 20, "rotation": 0}

    label               VARCHAR(255),
    color               VARCHAR(20) DEFAULT '#ff0000',
    notes               TEXT,
    is_visible          BOOLEAN DEFAULT TRUE,

    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT annotations_target CHECK (instance_id IS NOT NULL OR series_id IS NOT NULL)
);

CREATE INDEX idx_annotations_user ON annotations(user_id);
CREATE INDEX idx_annotations_instance ON annotations(instance_id) WHERE instance_id IS NOT NULL;
CREATE INDEX idx_annotations_series ON annotations(series_id) WHERE series_id IS NOT NULL;
CREATE INDEX idx_annotations_type ON annotations(annotation_type);
```

---

### `measurements`

Distance, area, and volume measurements on images.

```sql
CREATE TABLE measurements (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    series_id           INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,

    measurement_type    VARCHAR(50) NOT NULL,       -- 'distance', 'area', 'volume', 'angle', 'hounsfield'
    coordinates         JSONB NOT NULL,             -- Points defining the measurement
    -- Distance: {"start": {"x": 0, "y": 0, "z": 0}, "end": {"x": 10, "y": 10, "z": 0}}
    -- Area:     {"points": [...], "slice_index": 50}
    -- Volume:   {"contours": [{"slice": 0, "points": [...]}, ...]}
    -- Angle:    {"vertex": {...}, "arm1": {...}, "arm2": {...}}

    value               NUMERIC(15,4),              -- Calculated measurement
    unit                VARCHAR(20) NOT NULL,       -- 'mm', 'mm²', 'mm³', 'degrees', 'HU'

    label               VARCHAR(255),
    notes               TEXT,

    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_measurements_user ON measurements(user_id);
CREATE INDEX idx_measurements_series ON measurements(series_id);
CREATE INDEX idx_measurements_type ON measurements(measurement_type);
```

---

### `analysis_results`

AI/ML analysis outputs (lesion detection, volumetry, etc.).

```sql
CREATE TABLE analysis_results (
    id              SERIAL PRIMARY KEY,
    series_id       INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,

    analysis_type   VARCHAR(100) NOT NULL,          -- 'lesion_detection', 'volumetry', 'segmentation', etc.
    model_name      VARCHAR(255),                   -- Model identifier
    model_version   VARCHAR(50),                    -- Model version

    results         JSONB NOT NULL,                 -- Flexible result storage
    -- Lesion detection: {"lesions": [{"center": {...}, "size_mm": [10, 8, 12], "confidence": 0.95}]}
    -- Volumetry: {"total_volume_mm3": 1500.5, "regions": {...}}
    -- Segmentation: {"mask_s3_key": "...", "labels": {...}}

    confidence      NUMERIC(5,4),                   -- Overall confidence score
    status          VARCHAR(50) DEFAULT 'completed', -- 'pending', 'running', 'completed', 'failed'
    error_message   TEXT,

    requested_by    INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX idx_analysis_series ON analysis_results(series_id);
CREATE INDEX idx_analysis_type ON analysis_results(analysis_type);
CREATE INDEX idx_analysis_status ON analysis_results(status);
```

---

## Audit Tables

### `audit_log`

HIPAA-compliant logging of all system access for compliance and security.

```sql
CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ DEFAULT NOW(),
    user_id         INTEGER REFERENCES users(id),
    action          VARCHAR(100) NOT NULL,       -- 'login', 'logout', 'view_study', 'download', etc.
    resource_type   VARCHAR(50),                 -- 'study', 'series', 'instance', 'user', etc.
    resource_id     VARCHAR(255),                -- ID of the resource accessed
    ip_address      INET,                        -- Client IP address
    request_path    VARCHAR(500),                -- HTTP request path
    response_status INTEGER                      -- HTTP response status code
);

CREATE INDEX idx_audit_log_timestamp ON audit_log(timestamp DESC);
CREATE INDEX idx_audit_log_user ON audit_log(user_id);
CREATE INDEX idx_audit_log_action ON audit_log(action);
CREATE INDEX idx_audit_log_resource ON audit_log(resource_type, resource_id);
```

| Column | Type | Description |
|--------|------|-------------|
| `timestamp` | TIMESTAMPTZ | When the action occurred |
| `user_id` | INTEGER | User who performed the action (NULL for anonymous) |
| `action` | VARCHAR(100) | Action type (login, logout, view_study, download, etc.) |
| `resource_type` | VARCHAR(50) | Type of resource accessed |
| `resource_id` | VARCHAR(255) | ID of the resource |
| `ip_address` | INET | Client IP address |
| `request_path` | VARCHAR(500) | HTTP request path |
| `response_status` | INTEGER | HTTP response status code |

---

## Migration from SQLite

The current flat `dicom_files` table maps to the new normalized schema:

| Current Column | New Table | New Column |
|----------------|-----------|------------|
| PatientID | `patients` | patient_uid |
| PatientName | `patients` | name |
| PatientBirthDate | `patients` | birth_date |
| PatientSex | `patients` | sex |
| PatientWeight | `patients` | weight |
| StudyInstanceUID | `studies` | study_uid |
| StudyDescription | `studies` | description |
| StudyDate | `studies` | study_date |
| AccessionNumber | `studies` | accession |
| InstitutionName | `studies` | institution |
| SeriesInstanceUID | `series` | series_uid |
| SeriesDescription | `series` | description |
| SeriesNumber | `series` | series_number |
| Modality | `series` | modality |
| MRI params (TR, TE, etc.) | `series` | mri_params (JSONB) |
| Manufacturer | `series` | manufacturer |
| SliceThickness | `series` | slice_thickness |
| PixelSpacing | `series` | pixel_spacing |
| SOPInstanceUID | `instances` | sop_uid |
| InstanceNumber | `instances` | instance_number |
| **file_path** | `instances` | **s3_key** |
| SliceLocation | `instances` | slice_location |
| ImagePositionPatient | `instances` | image_position |
| ImageOrientationPatient | `instances` | image_orientation |
| Rows, Columns | `instances` | rows, columns |
| WindowCenter, WindowWidth | `instances` | window_center, window_width |

---

## Example Queries

### List all studies accessible to a user

```sql
SELECT s.id, s.study_uid, s.description, s.study_date, p.name as patient_name
FROM studies s
JOIN patients p ON s.patient_id = p.id
JOIN study_access sa ON s.id = sa.study_id
WHERE sa.user_id = $1
  AND (sa.expires_at IS NULL OR sa.expires_at > NOW())
ORDER BY s.study_date DESC;
```

### Get all series in a study with slice counts

```sql
SELECT
    se.id, se.series_uid, se.description, se.modality,
    se.mri_params->>'repetition_time' as tr,
    se.mri_params->>'echo_time' as te,
    se.instance_count
FROM series se
WHERE se.study_id = $1
ORDER BY se.series_number;
```

### Get ordered slices for volume reconstruction

```sql
SELECT
    i.sop_uid, i.instance_number, i.s3_key,
    i.slice_location, i.image_position,
    i.rows, i.columns, i.window_center, i.window_width
FROM instances i
WHERE i.series_id = $1
ORDER BY i.slice_location, i.instance_number;
```

### Get user's annotations for a series

```sql
SELECT
    a.id, a.annotation_type, a.coordinates, a.label, a.color,
    i.instance_number as slice_number
FROM annotations a
LEFT JOIN instances i ON a.instance_id = i.id
WHERE a.series_id = $1 AND a.user_id = $2 AND a.is_visible = TRUE
ORDER BY i.instance_number, a.created_at;
```

### Check if user has access to study

```sql
SELECT EXISTS(
    SELECT 1 FROM study_access
    WHERE user_id = $1 AND study_id = $2
      AND (expires_at IS NULL OR expires_at > NOW())
    UNION
    SELECT 1 FROM patient_access pa
    JOIN studies s ON s.patient_id = pa.patient_id
    WHERE pa.user_id = $1 AND s.id = $2
      AND (pa.expires_at IS NULL OR pa.expires_at > NOW())
) as has_access;
```

---

## Indexes Summary

| Table | Index | Columns | Purpose |
|-------|-------|---------|---------|
| patients | idx_patients_uid | patient_uid | Lookup by DICOM PatientID |
| patients | idx_patients_name | name | Search by patient name |
| studies | idx_studies_patient | patient_id | List studies for patient |
| studies | idx_studies_uid | study_uid | Lookup by StudyInstanceUID |
| studies | idx_studies_date | study_date DESC | Sort by study date |
| series | idx_series_study | study_id | List series in study |
| series | idx_series_uid | series_uid | Lookup by SeriesInstanceUID |
| series | idx_series_modality | modality | Filter by modality |
| instances | idx_instances_series | series_id | List instances in series |
| instances | idx_instances_number | series_id, instance_number | Ordered slice retrieval |
| instances | idx_instances_slice | series_id, slice_location | Spatial ordering |
| instances | idx_instances_s3 | s3_key | S3 key lookup |
| study_access | idx_study_access_user | user_id | User's accessible studies |
| annotations | idx_annotations_series | series_id | Annotations for series |
| measurements | idx_measurements_series | series_id | Measurements for series |

---

*Last Updated: 2026-02-01*
