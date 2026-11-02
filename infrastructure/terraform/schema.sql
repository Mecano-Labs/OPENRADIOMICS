-- OpenRadiomics Database Schema
-- PostgreSQL 15+
--
-- Apply with: psql -h <rds-endpoint> -U postgres -d openradiomics -f schema.sql

-- ============================================================================
-- DICOM HIERARCHY TABLES
-- ============================================================================

-- Patients table
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

-- Studies table
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

-- Series table
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

-- Instances table
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

-- ============================================================================
-- USER & AUTHENTICATION TABLES
-- ============================================================================

-- Users table
CREATE TABLE users (
    id              SERIAL PRIMARY KEY,
    cognito_sub     VARCHAR(255) UNIQUE NOT NULL,   -- Cognito user ID (sub claim)
    email           VARCHAR(255) UNIQUE,            -- Email (may be NULL if phone-only registration)
    display_name    VARCHAR(255),
    role            VARCHAR(50) DEFAULT 'viewer',   -- 'admin', 'clinician', 'researcher', 'viewer'
    user_type       VARCHAR(20) DEFAULT NULL,       -- 'patient', 'provider', NULL (not yet selected)

    -- Profile fields (HIPAA compliance)
    first_name      VARCHAR(100),
    last_name       VARCHAR(100),
    phone           VARCHAR(20),                    -- Phone number in E.164 format

    -- Address fields (HIPAA compliance)
    street_address  VARCHAR(255),
    city            VARCHAR(100),
    state           VARCHAR(100),
    postal_code     VARCHAR(20),
    country         VARCHAR(100),

    -- Consent tracking (HIPAA compliance)
    terms_accepted_at       TIMESTAMPTZ,            -- When user accepted Terms of Service
    hipaa_acknowledged_at   TIMESTAMPTZ,            -- When user acknowledged HIPAA requirements

    -- User preferences and metadata
    preferences     JSONB DEFAULT '{}',             -- User preferences (theme, defaults, etc.)
    last_login      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_users_cognito ON users(cognito_sub);
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_role ON users(role);
CREATE INDEX idx_users_phone ON users(phone);
CREATE INDEX idx_users_user_type ON users(user_type);

-- ============================================================================
-- ACCESS CONTROL TABLES
-- ============================================================================

-- Study access table
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

-- Patient access table
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

-- Share links table
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

-- ============================================================================
-- FEATURE TABLES
-- ============================================================================

-- Favorites table
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

-- Annotations table
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

-- Measurements table
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

-- Analysis results table
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

-- ============================================================================
-- AUDIT LOG TABLE (HIPAA Compliance)
-- ============================================================================

-- Audit log for tracking all access to the system
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

-- Indexes for efficient querying
CREATE INDEX idx_audit_log_timestamp ON audit_log(timestamp DESC);
CREATE INDEX idx_audit_log_user ON audit_log(user_id);
CREATE INDEX idx_audit_log_action ON audit_log(action);
CREATE INDEX idx_audit_log_resource ON audit_log(resource_type, resource_id);

-- Partition by month for better performance (optional, for high-volume systems)
-- This creates a partitioned table - uncomment if needed
-- CREATE TABLE audit_log (
--     id              BIGSERIAL,
--     timestamp       TIMESTAMPTZ DEFAULT NOW(),
--     user_id         INTEGER REFERENCES users(id),
--     action          VARCHAR(100) NOT NULL,
--     resource_type   VARCHAR(50),
--     resource_id     VARCHAR(255),
--     ip_address      INET,
--     request_path    VARCHAR(500),
--     response_status INTEGER,
--     PRIMARY KEY (id, timestamp)
-- ) PARTITION BY RANGE (timestamp);

-- ============================================================================
-- TRIGGER FOR updated_at
-- ============================================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_patients_updated_at
    BEFORE UPDATE ON patients
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_studies_updated_at
    BEFORE UPDATE ON studies
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_series_updated_at
    BEFORE UPDATE ON series
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_annotations_updated_at
    BEFORE UPDATE ON annotations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_measurements_updated_at
    BEFORE UPDATE ON measurements
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- SCHEMA VERSION
-- ============================================================================

CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TIMESTAMPTZ DEFAULT NOW(),
    description TEXT
);

INSERT INTO schema_version (version, description) VALUES (1, 'Initial schema - OpenRadiomics normalized DICOM database');
INSERT INTO schema_version (version, description) VALUES (2, 'Add audit_log table for HIPAA compliance');
INSERT INTO schema_version (version, description) VALUES (3, 'Add user profile fields for custom registration (name, phone, address, consent tracking)');
INSERT INTO schema_version (version, description) VALUES (4, 'Add pending_registrations table for verification tracking and rate limiting');
INSERT INTO schema_version (version, description) VALUES (5, 'Add user_type column for patient/provider classification');
INSERT INTO schema_version (version, description) VALUES (6, 'Add password_reset_tokens table for link-based password reset');
INSERT INTO schema_version (version, description) VALUES (7, 'Add trusted_devices table for MFA device trust feature');

-- ============================================================================
-- PENDING REGISTRATIONS TABLE (for verification code tracking & rate limiting)
-- ============================================================================

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
    form_data               JSONB,                          -- Store additional form data
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(email),
    UNIQUE(phone)
);

CREATE INDEX idx_pending_reg_email ON pending_registrations(email);
CREATE INDEX idx_pending_reg_phone ON pending_registrations(phone);
CREATE INDEX idx_pending_reg_status ON pending_registrations(status);
CREATE INDEX idx_pending_reg_expires ON pending_registrations(code_expires_at);

CREATE TRIGGER update_pending_registrations_updated_at
    BEFORE UPDATE ON pending_registrations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- PASSWORD RESET TOKENS TABLE (for link-based password reset)
-- ============================================================================

CREATE TABLE password_reset_tokens (
    id              SERIAL PRIMARY KEY,
    email           VARCHAR(255) NOT NULL,
    token           VARCHAR(64) UNIQUE NOT NULL,    -- Secure random token
    expires_at      TIMESTAMPTZ NOT NULL,           -- 1 hour expiry
    used_at         TIMESTAMPTZ,                    -- When token was used (NULL if unused)
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_password_reset_token ON password_reset_tokens(token);
CREATE INDEX idx_password_reset_email ON password_reset_tokens(email);
CREATE INDEX idx_password_reset_expires ON password_reset_tokens(expires_at);

-- ============================================================================
-- TRUSTED DEVICES TABLE (for MFA device trust feature)
-- ============================================================================

CREATE TABLE trusted_devices (
    id              SERIAL PRIMARY KEY,
    user_email      VARCHAR(255) NOT NULL,
    device_token    VARCHAR(64) UNIQUE NOT NULL,
    device_name     VARCHAR(255),
    user_agent      TEXT,
    ip_address      INET,
    trusted_at      TIMESTAMPTZ DEFAULT NOW(),
    last_used_at    TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    is_active       BOOLEAN DEFAULT TRUE
);

CREATE INDEX idx_trusted_devices_token ON trusted_devices(device_token);
CREATE INDEX idx_trusted_devices_email ON trusted_devices(user_email);
CREATE INDEX idx_trusted_devices_expires ON trusted_devices(expires_at) WHERE is_active = TRUE;
