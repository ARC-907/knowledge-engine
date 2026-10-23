-- Pipeline DB created by the pre-versioned inline schema of
-- runtime/foundation/db.py (train/08-core-utils, before the split into
-- numbered migrations), dumped with sqlite3.Connection.iterdump() minus
-- the virtual-table rows (i.e. what `sqlite3 x.db .dump` writes). Used by
-- tests/test_foundation_migrations.py to prove in-place upgrades. Do not edit.
BEGIN TRANSACTION;
CREATE TABLE agent_api_keys (
            key_id TEXT PRIMARY KEY,
            key_hash TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT 'admin',
            enabled INTEGER DEFAULT 1,
            is_master INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_used_at TEXT,
            expires_at TEXT,
            notes TEXT
        );
CREATE TABLE agent_key_permissions (
            perm_id TEXT PRIMARY KEY,
            key_id TEXT NOT NULL REFERENCES agent_api_keys(key_id) ON DELETE CASCADE,
            resource_type TEXT NOT NULL,
            resource_id TEXT NOT NULL DEFAULT '*',
            permission TEXT NOT NULL DEFAULT 'invoke',
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );
CREATE TABLE api_keys (
            key_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            display_name TEXT NOT NULL,
            env_var TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            last_verified TEXT,
            notes TEXT
        );
CREATE TABLE batch_items (
            item_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES batch_jobs(batch_id),
            seq INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            input_json TEXT NOT NULL DEFAULT '{}',
            output_json TEXT,
            assigned_node TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT
        );
CREATE TABLE batch_jobs (
            batch_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_by TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            total_items INTEGER DEFAULT 0,
            completed_items INTEGER DEFAULT 0,
            failed_items INTEGER DEFAULT 0,
            config_json TEXT DEFAULT '{}',
            error TEXT
        );
CREATE TABLE board_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            engine_port INTEGER NOT NULL DEFAULT 9210,
            standalone_port INTEGER NOT NULL DEFAULT 11437,
            sweep_interval_s INTEGER NOT NULL DEFAULT 60,
            stale_blocker_hours INTEGER NOT NULL DEFAULT 2,
            digest_interval_minutes INTEGER NOT NULL DEFAULT 60,
            max_messages_before_prune INTEGER NOT NULL DEFAULT 5000,
            default_ttl_hours INTEGER NOT NULL DEFAULT 168,
            sweeper_enabled INTEGER NOT NULL DEFAULT 1,
            require_key_for_post INTEGER NOT NULL DEFAULT 0,
            channels_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL
        );
INSERT INTO "board_config" VALUES(1,9210,11437,60,2,60,5000,168,1,0,'["ops", "research", "project", "worktree", "branch", "library", "planning", "execution", "testing", "chatter"]','2026-01-01T00:00:00+00:00');
CREATE TABLE board_sweeps (
            sweep_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            pruned_expired INTEGER DEFAULT 0,
            pruned_overflow INTEGER DEFAULT 0,
            reminders_emitted INTEGER DEFAULT 0,
            digests_emitted INTEGER DEFAULT 0,
            error TEXT
        );
CREATE TABLE chat_conversations (
            conversation_id TEXT PRIMARY KEY,
            title TEXT,
            model_id TEXT,
            provider TEXT,
            node_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived INTEGER DEFAULT 0
        , system_prompt TEXT, context_limit INTEGER DEFAULT 20, pinned INTEGER DEFAULT 0, persona_id TEXT);
CREATE TABLE chat_messages (
            chat_msg_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES chat_conversations(conversation_id),
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            model_id TEXT,
            token_count INTEGER,
            created_at TEXT NOT NULL
        , excluded_from_context INTEGER DEFAULT 0, bookmarked INTEGER DEFAULT 0);
CREATE TABLE chat_personas (
            persona_id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            icon TEXT DEFAULT '',
            system_prompt TEXT NOT NULL,
            suggested_starters TEXT DEFAULT '[]',
            sort_order INTEGER DEFAULT 0,
            is_default INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE chat_prompt_templates (
            template_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            body TEXT NOT NULL,
            variables TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE context_artifacts (
            artifact_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            content TEXT NOT NULL,
            source_message_start TEXT,
            source_message_end TEXT,
            active INTEGER DEFAULT 1,
            created_by TEXT,
            meta_json TEXT,
            created_at TEXT NOT NULL
        );
CREATE TABLE events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            task_id TEXT,
            node_id TEXT,
            agent_id TEXT,
            detail TEXT,
            data_json TEXT
        );
CREATE TABLE folder_mounts (
            mount_id TEXT PRIMARY KEY,
            mount_path TEXT NOT NULL UNIQUE,
            local_path TEXT NOT NULL,
            node_id TEXT NOT NULL,
            read_only INTEGER DEFAULT 1,
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            notes TEXT
        );
CREATE TABLE folder_permissions (
            perm_id TEXT PRIMARY KEY,
            folder_path TEXT NOT NULL,
            principal_type TEXT NOT NULL,
            principal_value TEXT NOT NULL,
            permission TEXT NOT NULL DEFAULT 'read',
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            notes TEXT
        );
CREATE TABLE hosted_tools (
            tool_id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            description TEXT,
            route TEXT NOT NULL UNIQUE,
            input_schema TEXT DEFAULT '{}',
            output_schema TEXT DEFAULT '{}',
            command TEXT,
            working_dir TEXT,
            timeout_seconds INTEGER DEFAULT 30,
            upstream_url TEXT,
            health_endpoint TEXT,
            local_path TEXT,
            node_id TEXT,
            enabled INTEGER DEFAULT 1,
            tags TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_invoked_at TEXT,
            invocation_count INTEGER DEFAULT 0,
            allow_inline INTEGER DEFAULT 0,
            allowed_methods TEXT,
            allowed_headers TEXT
        );
INSERT INTO "hosted_tools" VALUES('legacy-tool-1','legacy-tool','static',NULL,'legacy','{}','{}',NULL,NULL,30,NULL,NULL,NULL,NULL,1,'[]','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',NULL,0,0,NULL,NULL);
CREATE TABLE kv_store (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE messages (
            message_id TEXT PRIMARY KEY,
            channel TEXT NOT NULL DEFAULT 'ops',
            thread_id TEXT,
            task_id TEXT,
            product_id TEXT,
            sender_agent_id TEXT,
            sender_node_id TEXT,
            sender_role TEXT,
            model_id TEXT,
            message_type TEXT NOT NULL,
            subject TEXT,
            body TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            visibility_scope TEXT DEFAULT 'all',
            requires_ack INTEGER DEFAULT 0,
            ack_by TEXT DEFAULT '[]',
            reply_to TEXT,
            correlation_id TEXT
        );
INSERT INTO "messages" VALUES('legacy-msg-1','ops',NULL,NULL,NULL,'fixture-node','fixture-node',NULL,NULL,'status_update','legacy subject','legacy body text','2026-01-01T00:00:00+00:00',NULL,'all',0,'[]',NULL,NULL);
PRAGMA writable_schema=ON;
INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql)VALUES('table','messages_fts','messages_fts',0,'CREATE VIRTUAL TABLE messages_fts USING fts5(
                message_id UNINDEXED,
                channel,
                subject,
                body
            )');
CREATE TABLE 'messages_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
INSERT INTO "messages_fts_config" VALUES('version',4);
CREATE TABLE 'messages_fts_content'(id INTEGER PRIMARY KEY, c0, c1, c2, c3);
INSERT INTO "messages_fts_content" VALUES(1,'legacy-msg-1','ops','legacy subject','legacy body text');
CREATE TABLE 'messages_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
INSERT INTO "messages_fts_data" VALUES(1,X'0100010203');
INSERT INTO "messages_fts_data" VALUES(10,X'000000000102020002010101020101');
INSERT INTO "messages_fts_data" VALUES(137438953473,X'000000420530626F6479010601030301066C6567616379010C01020201030201036F7073010601010201077375626A65637401060102030104746578740106010304040B100A0E');
INSERT INTO "messages_fts_data" VALUES(274877906945,X'000000420530626F6479010701030301066C6567616379010D01020201030201036F7073010701010201077375626A65637401070102030104746578740107010304040B100A0E');
CREATE TABLE 'messages_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
INSERT INTO "messages_fts_docsize" VALUES(1,X'00010203');
CREATE TABLE 'messages_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
INSERT INTO "messages_fts_idx" VALUES(1,X'',2);
INSERT INTO "messages_fts_idx" VALUES(2,X'',2);
CREATE TABLE pipeline_runs (
            pipeline_id TEXT PRIMARY KEY,
            pipeline_card_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            input_json TEXT NOT NULL DEFAULT '{}',
            stages_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT
        );
CREATE TABLE prompt_guards (
            guard_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            rule_type TEXT NOT NULL,
            pattern TEXT,
            action TEXT NOT NULL DEFAULT 'block',
            scope TEXT DEFAULT 'all',
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            notes TEXT
        );
CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            task_type TEXT NOT NULL,
            library TEXT NOT NULL,
            target_path TEXT NOT NULL DEFAULT '',
            chapter_key TEXT NOT NULL,
            priority INTEGER DEFAULT 3,
            depends_on TEXT DEFAULT '[]',
            allowed_layer TEXT DEFAULT 'local',
            assigned_capability TEXT DEFAULT '[]',
            source_policy TEXT DEFAULT '{}',
            input_json TEXT NOT NULL DEFAULT '{}',
            meta_json TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            assigned_worker TEXT,
            lease_expires_at TEXT,
            claimed_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            retry_count INTEGER DEFAULT 0,
            failure_reason TEXT,
            output_artifacts TEXT DEFAULT '[]',
            notes TEXT,
            audit_trail TEXT DEFAULT '[]'
        );
INSERT INTO "tasks" VALUES('legacy-task-1','audit','lib','','ch1',3,'[]','local','[]','{}','{}',NULL,'queued',NULL,NULL,NULL,NULL,NULL,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',0,NULL,'[]',NULL,'[]');
CREATE TABLE workers (
            node_id TEXT PRIMARY KEY,
            hostname TEXT,
            tailscale_ip TEXT,
            display_name TEXT,
            model_class TEXT,
            role TEXT,
            capabilities TEXT DEFAULT '[]',
            ollama_endpoint TEXT,
            ollama_model TEXT,
            max_concurrent INTEGER DEFAULT 1,
            status TEXT DEFAULT 'offline',
            current_tasks TEXT DEFAULT '[]',
            last_heartbeat TEXT,
            registered_at TEXT,
            config_json TEXT DEFAULT '{}',
            safeguard_locked INTEGER DEFAULT 0,
            safeguard_reason TEXT,
            resources_json TEXT DEFAULT '{}'
        );
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_priority ON tasks(priority);
CREATE INDEX idx_tasks_library ON tasks(library);
CREATE INDEX idx_messages_task ON messages(task_id);
CREATE INDEX idx_messages_type ON messages(message_type);
CREATE INDEX idx_messages_created ON messages(created_at);
CREATE INDEX idx_messages_channel ON messages(channel);
CREATE INDEX idx_messages_type_created
            ON messages(message_type, created_at);
CREATE INDEX idx_messages_reply_to
            ON messages(reply_to);
CREATE INDEX idx_events_task ON events(task_id);
CREATE INDEX idx_events_type ON events(event_type);
CREATE INDEX idx_batch_status ON batch_jobs(status);
CREATE INDEX idx_batchitems_batch ON batch_items(batch_id);
CREATE INDEX idx_batchitems_status ON batch_items(status);
CREATE INDEX idx_tools_kind ON hosted_tools(kind);
CREATE INDEX idx_tools_route ON hosted_tools(route);
CREATE INDEX idx_fperm_folder ON folder_permissions(folder_path);
CREATE INDEX idx_chatconv_updated ON chat_conversations(updated_at);
CREATE INDEX idx_chatmsg_conv ON chat_messages(conversation_id);
CREATE INDEX idx_chatmsg_created ON chat_messages(created_at);
CREATE INDEX idx_agentkeys_hash ON agent_api_keys(key_hash);
CREATE INDEX idx_agentkeys_enabled ON agent_api_keys(enabled);
CREATE UNIQUE INDEX idx_agentkeys_one_master
            ON agent_api_keys(is_master)
            WHERE is_master = 1 AND enabled = 1;
CREATE INDEX idx_akperm_key ON agent_key_permissions(key_id);
CREATE INDEX idx_akperm_resource ON agent_key_permissions(resource_type, resource_id);
CREATE INDEX idx_artifacts_conv ON context_artifacts(conversation_id);
CREATE INDEX idx_pipeline_status ON pipeline_runs(status);
CREATE INDEX idx_sweeps_started ON board_sweeps(started_at);
CREATE TRIGGER messages_fts_ai
            AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(message_id, channel, subject, body)
                VALUES (new.message_id, new.channel, COALESCE(new.subject,''), COALESCE(new.body,''));
            END;
CREATE TRIGGER messages_fts_ad
            AFTER DELETE ON messages BEGIN
                DELETE FROM messages_fts WHERE message_id = old.message_id;
            END;
CREATE TRIGGER messages_fts_au
            AFTER UPDATE OF subject, body, channel ON messages BEGIN
                DELETE FROM messages_fts WHERE message_id = old.message_id;
                INSERT INTO messages_fts(message_id, channel, subject, body)
                VALUES (new.message_id, new.channel, COALESCE(new.subject,''), COALESCE(new.body,''));
            END;
PRAGMA writable_schema=OFF;
DELETE FROM "sqlite_sequence";
COMMIT;
