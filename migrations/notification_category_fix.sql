UPDATE notifications AS n
JOIN alerts AS a ON a.id = n.alert_id
SET n.category = a.level,
    n.event_type = CASE a.level
        WHEN 'URGENT' THEN 'MISSED'
        WHEN 'CHECK' THEN 'NEEDS_CHECK'
    END
WHERE a.level IN ('URGENT', 'CHECK');

UPDATE notifications
SET category = 'CHECK'
WHERE event_type = 'NOT_STARTED';
