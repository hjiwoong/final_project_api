ALTER TABLE attendances
    ADD COLUMN reason_type ENUM('SICK', 'FAMILY', 'FIELD_TRIP', 'OTHER') NULL,
    ADD COLUMN reason_memo TEXT NULL,
    ADD COLUMN status ENUM('REQUESTED', 'CONFIRMED', 'REJECTED') NULL,
    ADD COLUMN confirmed_by INT NULL,
    ADD COLUMN confirmed_at DATETIME NULL,
    ADD COLUMN reject_reason VARCHAR(200) NULL,
    ADD CONSTRAINT fk_attendances_confirmed_by_users
        FOREIGN KEY (confirmed_by) REFERENCES users(id) ON DELETE SET NULL;

CREATE INDEX ix_attendances_status ON attendances (status);

UPDATE attendances
SET status = 'REQUESTED'
WHERE is_absent = 1 AND status IS NULL;
