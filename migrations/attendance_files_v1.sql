CREATE TABLE IF NOT EXISTS attendance_files (
    id INT NOT NULL AUTO_INCREMENT,
    attendance_id INT NOT NULL,
    stored_name VARCHAR(64) NOT NULL,
    original_name VARCHAR(255) NOT NULL,
    content_type VARCHAR(50) NOT NULL,
    size INT NOT NULL,
    uploaded_by INT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_attendance_files_stored_name (stored_name),
    KEY ix_attendance_files_attendance_id (attendance_id),
    CONSTRAINT fk_attendance_files_attendance_id_attendances
        FOREIGN KEY (attendance_id) REFERENCES attendances(id) ON DELETE CASCADE,
    CONSTRAINT fk_attendance_files_uploaded_by_users
        FOREIGN KEY (uploaded_by) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
