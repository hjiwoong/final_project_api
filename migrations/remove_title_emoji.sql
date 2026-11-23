SELECT COUNT(*) FROM notifications WHERE title LIKE '%🚨%' OR title LIKE '%⚠️%';

UPDATE notifications
SET title = TRIM(REPLACE(REPLACE(title, '🚨', ''), '⚠️', ''))
WHERE title LIKE '%🚨%' OR title LIKE '%⚠️%';
