ALTER TABLE messages ADD COLUMN content_hash TEXT DEFAULT NULL;
ALTER TABLE messages ADD COLUMN file_unique_id TEXT DEFAULT NULL;
ALTER TABLE messages ADD COLUMN image_phash TEXT DEFAULT NULL;
ALTER TABLE messages ADD COLUMN image_dhash TEXT DEFAULT NULL;
ALTER TABLE messages ADD COLUMN image_rgb BLOB DEFAULT NULL CHECK (image_rgb IS NULL OR length(image_rgb) = 49152);
ALTER TABLE messages ADD COLUMN publication_channel_id INTEGER DEFAULT NULL;
CREATE INDEX IF NOT EXISTS messages_file_unique_id ON messages(file_unique_id) WHERE file_unique_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS messages_content_hash ON messages(content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS messages_image_candidates ON messages(image_phash,image_dhash,message_id,chat_id,content_hash) WHERE image_phash IS NOT NULL AND image_dhash IS NOT NULL;
