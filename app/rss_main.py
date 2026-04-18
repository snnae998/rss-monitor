#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS监控程序 v4.0 - 智能匹配增强版
新增功能：
1. 推送去重（标题相似度过滤）
2. 推送内容预览（显示正文摘要）
3. 源健康检查（连续失败自动禁用）
4. 抓取时自动修正时区（UTC+0 → UTC+8）
5. 智能关键词匹配（标题+摘要+标签，大幅提升命中率）
6. 版块识别支持（为后续版块过滤做准备）
"""

import os
import sys
import time
import json
import sqlite3
import logging
import hashlib
import random
import re
import shutil
from datetime import datetime, timedelta
from threading import Thread, Lock
from collections import defaultdict
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime

import feedparser
import requests
from logging.handlers import RotatingFileHandler

# ============= 配置路径 =============
DATA_DIR = '/data'
os.makedirs(DATA_DIR, exist_ok=True)

DB_FILE = os.path.join(DATA_DIR, 'monitor.db')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
LOG_FILE = os.path.join(DATA_DIR, 'monitor.log')
PID_FILE = os.path.join(DATA_DIR, 'monitor.pid')

os.makedirs(BACKUP_DIR, exist_ok=True)

# 默认 User-Agent
DEFAULT_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# 相似度阈值（0-1，越大越严格）
SIMILARITY_THRESHOLD = 0.8

# 源健康检查配置
MAX_CONSECUTIVE_ERRORS = 5
HEALTH_CHECK_ENABLED = True

# ============= 版块映射表（移植自 nodeseek-rss-telegram-bot）=============
CATEGORY_LABELS: dict[str, str] = {
    "daily": "日常",
    "tech": "技术",
    "info": "情报",
    "review": "测评",
    "trade": "交易",
    "carpool": "拼车",
    "promo": "推广",
    "life": "生活",
    "dev": "Dev",
    "photo-share": "贴图",
    "expose": "曝光",
    "inner": "内版",
    "sandbox": "沙盒",
}

CATEGORY_ALIASES: dict[str, str] = {
    "日常": "daily",
    "技术": "tech",
    "情报": "info",
    "测评": "review",
    "交易": "trade",
    "拼车": "carpool",
    "推广": "promo",
    "promotion": "promo",
    "生活": "life",
    "dev": "dev",
    "贴图": "photo-share",
    "曝光": "expose",
    "内版": "inner",
    "沙盒": "sandbox",
}


def normalize_category_slug(value: str | None) -> str | None:
    """将版块名称标准化为英文标识"""
    if not value:
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if cleaned in CATEGORY_LABELS:
        return cleaned
    return CATEGORY_ALIASES.get(value.strip()) or CATEGORY_ALIASES.get(cleaned)


def category_label(slug: str | None) -> str:
    """获取版块的中文名称"""
    if not slug:
        return "未分类"
    return CATEGORY_LABELS.get(slug, slug)


# ============= 日志配置 =============
log_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[log_handler]
)
logger = logging.getLogger(__name__)


# ============= 辅助函数 =============
def is_similar_title(title1, title2, threshold=SIMILARITY_THRESHOLD):
    """判断两个标题是否相似"""
    if not title1 or not title2:
        return False
    clean1 = re.sub(r'\s+', '', title1.lower())
    clean2 = re.sub(r'\s+', '', title2.lower())
    if len(clean1) < 5 or len(clean2) < 5:
        return clean1 == clean2
    return SequenceMatcher(None, clean1, clean2).ratio() >= threshold


def extract_content_preview(entry, max_length=100):
    """提取内容预览"""
    content = ''
    if hasattr(entry, 'summary'):
        content = entry.summary
    elif hasattr(entry, 'description'):
        content = entry.description
    elif hasattr(entry, 'content'):
        content = entry.content[0].value if entry.content else ''

    content = re.sub(r'<[^>]+>', ' ', content)
    content = re.sub(r'\s+', ' ', content).strip()

    if len(content) > max_length:
        content = content[:max_length] + '...'
    return content


# ============= 全局状态 =============
class MonitorState:
    def __init__(self):
        self.running = True
        self.paused = False
        self.pause_reason = ""
        self.pause_time = None
        self.resume_time = None
        self.last_check = None
        self.total_checks = 0
        self.total_matches = 0
        self.lock = Lock()
        self.keyword_notify = True
        self.system_notify = True
        self.error_notify = True
        self.global_interval = 60
        self.global_timeout = 30

    def pause(self, reason="手动暂停", duration_minutes=None):
        with self.lock:
            if self.paused:
                return False, "监控已在暂停状态"
            self.paused = True
            self.pause_reason = reason
            self.pause_time = datetime.now()
            if duration_minutes:
                self.resume_time = self.pause_time + timedelta(minutes=duration_minutes)
            return True, f"监控已暂停: {reason}"

    def resume(self):
        with self.lock:
            if not self.paused:
                return False, "监控未在暂停状态"
            self.paused = False
            self.pause_reason = ""
            self.resume_time = None
            return True, "监控已恢复"

    def get_status_text(self):
        with self.lock:
            if not self.running:
                return "⚫ 已停止"
            elif self.paused:
                return f"⏸ 已暂停 - {self.pause_reason}"
            else:
                return "🟢 运行中"


monitor_state = MonitorState()
user_states = {}


# ============= 辅助函数 =============
def auto_delete_message(bot, msg_id, delay=5):
    time.sleep(delay)
    bot.delete_message(msg_id)


def send_and_auto_delete(bot, text, reply_to=None, delay=5):
    success, msg_id = bot.send_message(text, reply_to=reply_to)
    if success and msg_id:
        Thread(target=auto_delete_message, args=(bot, msg_id, delay), daemon=True).start()
    return success, msg_id


# ============= 数据库管理 =============
class Database:
    def __init__(self, db_path=DB_FILE):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_tables()
        self.upgrade_tables()
        self.init_default_sources()
        self.migrate_from_json()

    def init_tables(self):
        cursor = self.conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS rss_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                url TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                check_interval INTEGER DEFAULT 60,
                last_check TIMESTAMP,
                last_status TEXT,
                error_count INTEGER DEFAULT 0,
                consecutive_errors INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT UNIQUE NOT NULL,
                group_name TEXT DEFAULT '默认',
                enabled INTEGER DEFAULT 1,
                match_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS keyword_filters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword_id INTEGER NOT NULL,
                filter_type TEXT NOT NULL,
                filter_value TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE,
                UNIQUE(keyword_id, filter_type, filter_value)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unique_key TEXT UNIQUE,
                source_name TEXT,
                title TEXT,
                author TEXT,
                link TEXT,
                matched_keywords TEXT,
                content_preview TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE UNIQUE,
                total_checks INTEGER DEFAULT 0,
                total_matches INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pause_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT,
                reason TEXT,
                duration_minutes INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        self.conn.commit()

    def upgrade_tables(self):
        cursor = self.conn.cursor()

        cursor.execute("PRAGMA table_info(rss_sources)")
        columns = [col[1] for col in cursor.fetchall()]

        if 'timeout' not in columns:
            try:
                cursor.execute("ALTER TABLE rss_sources ADD COLUMN timeout INTEGER DEFAULT 30")
                logger.info("✅ 已添加 rss_sources.timeout 字段")
            except Exception as e:
                logger.warning(f"添加 timeout 字段失败: {e}")

        if 'user_agent' not in columns:
            try:
                cursor.execute("ALTER TABLE rss_sources ADD COLUMN user_agent TEXT")
                logger.info("✅ 已添加 rss_sources.user_agent 字段")
            except Exception as e:
                logger.warning(f"添加 user_agent 字段失败: {e}")

        if 'consecutive_errors' not in columns:
            try:
                cursor.execute("ALTER TABLE rss_sources ADD COLUMN consecutive_errors INTEGER DEFAULT 0")
                logger.info("✅ 已添加 rss_sources.consecutive_errors 字段")
            except Exception as e:
                logger.warning(f"添加 consecutive_errors 字段失败: {e}")

        cursor.execute("PRAGMA table_info(notifications)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'content_preview' not in columns:
            try:
                cursor.execute("ALTER TABLE notifications ADD COLUMN content_preview TEXT")
                logger.info("✅ 已添加 notifications.content_preview 字段")
            except Exception as e:
                logger.warning(f"添加 content_preview 字段失败: {e}")

        self.conn.commit()

    def init_default_sources(self):
        return

    def migrate_from_json(self):
        json_file = CONFIG_FILE
        if not os.path.exists(json_file):
            return
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            cursor = self.conn.cursor()
            for keyword in config.get('keywords', []):
                try:
                    cursor.execute("INSERT OR IGNORE INTO keywords (keyword) VALUES (?)", (keyword,))
                except:
                    pass
            for key, entry in config.get('notified_entries', {}).items():
                if isinstance(entry, dict):
                    try:
                        cursor.execute('''
                            INSERT OR IGNORE INTO notifications
                            (unique_key, source_name, title, author, link, matched_keywords, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (key, 'NodeSeek', entry.get('title', ''), entry.get('author', ''),
                              entry.get('link', ''), ','.join(entry.get('keywords', [])),
                              entry.get('time', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))))
                    except:
                        pass
            self.conn.commit()
            os.rename(json_file, json_file + '.migrated')
            logger.info("✅ 数据迁移完成")
        except Exception as e:
            logger.error(f"数据迁移失败: {e}")

    # ============= 关键词操作 =============
    def add_keyword(self, keyword, group='默认'):
        try:
            cursor = self.conn.cursor()
            cursor.execute("INSERT INTO keywords (keyword, group_name) VALUES (?, ?)", (keyword, group))
            self.conn.commit()
            return True, f"✅ 关键词 '{keyword}' 已添加"
        except sqlite3.IntegrityError:
            return False, f"❌ 关键词 '{keyword}' 已存在"

    def delete_keyword(self, keyword):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM keywords WHERE keyword = ?", (keyword,))
        self.conn.commit()
        return cursor.rowcount > 0

    def get_all_keywords(self, enabled_only=False):
        cursor = self.conn.cursor()
        if enabled_only:
            cursor.execute("SELECT * FROM keywords WHERE enabled = 1 ORDER BY created_at DESC")
        else:
            cursor.execute("SELECT * FROM keywords ORDER BY enabled DESC, created_at DESC")
        return cursor.fetchall()

    def toggle_keyword(self, keyword_id, enabled):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE keywords SET enabled = ? WHERE id = ?", (1 if enabled else 0, keyword_id))
        self.conn.commit()

    def get_keywords_by_group(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, group_name, keyword, enabled FROM keywords ORDER BY group_name, keyword")
        groups = defaultdict(list)
        for row in cursor.fetchall():
            groups[row['group_name']].append({
                'id': row['id'],
                'keyword': row['keyword'],
                'enabled': row['enabled']
            })
        return dict(groups)

    def import_keywords(self, keywords_data):
        cursor = self.conn.cursor()
        added = 0
        skipped = 0
        for item in keywords_data:
            keyword = item.get('keyword', '').strip()
            group = item.get('group', '默认')
            enabled = item.get('enabled', 1)
            if not keyword:
                continue
            try:
                cursor.execute(
                    "INSERT INTO keywords (keyword, group_name, enabled) VALUES (?, ?, ?)",
                    (keyword, group, enabled)
                )
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1
        self.conn.commit()
        return added, skipped

    def export_keywords(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT keyword, group_name, enabled, match_count
            FROM keywords
            ORDER BY group_name, keyword
        ''')
        return [dict(row) for row in cursor.fetchall()]

    # ============= 过滤规则操作 =============
    def add_filter(self, keyword_id, filter_type, filter_value):
        try:
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT INTO keyword_filters (keyword_id, filter_type, filter_value)
                VALUES (?, ?, ?)
            ''', (keyword_id, filter_type, filter_value))
            self.conn.commit()
            return True, f"✅ 过滤规则已添加"
        except sqlite3.IntegrityError:
            return False, f"❌ 过滤规则已存在"

    def delete_filter(self, filter_id):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM keyword_filters WHERE id = ?", (filter_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    def get_filters(self, keyword_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM keyword_filters
            WHERE keyword_id = ? AND enabled = 1
            ORDER BY filter_type, created_at
        ''', (keyword_id,))
        return cursor.fetchall()

    def get_all_filters(self, keyword_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM keyword_filters
            WHERE keyword_id = ?
            ORDER BY filter_type, enabled DESC, created_at
        ''', (keyword_id,))
        return cursor.fetchall()

    def toggle_filter(self, filter_id, enabled):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE keyword_filters SET enabled = ? WHERE id = ?",
                       (1 if enabled else 0, filter_id))
        self.conn.commit()

    # ============= RSS源操作 =============
    def get_enabled_sources(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM rss_sources WHERE enabled = 1")
        return cursor.fetchall()

    def get_all_sources(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM rss_sources ORDER BY enabled DESC, name")
        return cursor.fetchall()

    def toggle_source(self, source_id, enabled):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE rss_sources SET enabled = ? WHERE id = ?", (1 if enabled else 0, source_id))
        self.conn.commit()

    def delete_source(self, source_id):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM rss_sources WHERE id = ?", (source_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    def add_source(self, name, url, interval=120, timeout=30, user_agent=None):
        try:
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT INTO rss_sources (name, url, check_interval, timeout, user_agent)
                VALUES (?, ?, ?, ?, ?)
            ''', (name, url, interval, timeout, user_agent))
            self.conn.commit()
            return True, f"✅ RSS源 '{name}' 已添加"
        except sqlite3.IntegrityError:
            return False, f"❌ RSS源 '{name}' 已存在"

    def update_source_status(self, source_id, status, error=False):
        cursor = self.conn.cursor()
        if error:
            cursor.execute('''
                UPDATE rss_sources
                SET last_check = ?, last_status = ?,
                    error_count = error_count + 1,
                    consecutive_errors = consecutive_errors + 1
                WHERE id = ?
            ''', (datetime.now(), status, source_id))
        else:
            cursor.execute('''
                UPDATE rss_sources
                SET last_check = ?, last_status = ?, consecutive_errors = 0
                WHERE id = ?
            ''', (datetime.now(), status, source_id))
        self.conn.commit()

    def check_and_disable_unhealthy_sources(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT id, name, consecutive_errors FROM rss_sources
            WHERE enabled = 1 AND consecutive_errors >= ?
        ''', (MAX_CONSECUTIVE_ERRORS,))
        unhealthy = cursor.fetchall()

        for src in unhealthy:
            cursor.execute("UPDATE rss_sources SET enabled = 0 WHERE id = ?", (src['id'],))
            logger.warning(f"⚠️ 源 {src['name']} 连续失败 {src['consecutive_errors']} 次，已自动禁用")

        self.conn.commit()
        return len(unhealthy)

    # ============= 通知操作 =============
    def is_notified(self, unique_key):
        cursor = self.conn.cursor()
        cursor.execute("SELECT id FROM notifications WHERE unique_key = ?", (unique_key,))
        return cursor.fetchone() is not None

    def get_recent_titles(self, limit=50):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT title FROM notifications
            ORDER BY created_at DESC LIMIT ?
        ''', (limit,))
        return [row[0] for row in cursor.fetchall()]

    def add_notification(self, unique_key, source_name, title, author, link, matched_keywords, content_preview='', created_at=None):
        try:
            cursor = self.conn.cursor()
            if created_at is None:
                created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute('''
                INSERT INTO notifications
                (unique_key, source_name, title, author, link, matched_keywords, content_preview, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (unique_key, source_name, title, author, link, ','.join(matched_keywords), content_preview, created_at))
            self.conn.commit()
            for kw in matched_keywords:
                cursor.execute("UPDATE keywords SET match_count = match_count + 1 WHERE keyword = ?", (kw,))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    # ============= 统计操作 =============
    def get_stats(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM keywords WHERE enabled = 1")
        total_keywords = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM notifications")
        total_notifications = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM rss_sources WHERE enabled = 1")
        total_sources = cursor.fetchone()[0]
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute("SELECT COUNT(*) FROM notifications WHERE date(created_at) = ?", (today,))
        today_matches = cursor.fetchone()[0]
        week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        cursor.execute("SELECT COUNT(*) FROM notifications WHERE date(created_at) >= ?", (week_ago,))
        week_matches = cursor.fetchone()[0]
        cursor.execute('''
            SELECT keyword, match_count FROM keywords
            WHERE enabled = 1 AND match_count > 0
            ORDER BY match_count DESC LIMIT 5
        ''')
        top_keywords = cursor.fetchall()
        return {
            'total_keywords': total_keywords,
            'total_notifications': total_notifications,
            'total_sources': total_sources,
            'today_matches': today_matches,
            'week_matches': week_matches,
            'top_keywords': top_keywords
        }

    def get_data_counts(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM notifications")
        notif_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM daily_stats")
        stats_count = cursor.fetchone()[0]
        return {'notifications': notif_count, 'stats': stats_count}

    def record_daily_stats(self):
        today = datetime.now().strftime('%Y-%m-%d')
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM notifications WHERE date(created_at) = ?", (today,))
        today_matches = cursor.fetchone()[0]
        cursor.execute('''
            INSERT INTO daily_stats (date, total_checks, total_matches)
            VALUES (?, 1, ?)
            ON CONFLICT(date) DO UPDATE SET
                total_checks = total_checks + 1,
                total_matches = ?
        ''', (today, today_matches, today_matches))
        self.conn.commit()

    def add_pause_record(self, action, reason, duration=None):
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO pause_history (action, reason, duration_minutes) VALUES (?, ?, ?)",
                       (action, reason, duration))
        self.conn.commit()

    def cleanup_old_notifications(self, days=30):
        cursor = self.conn.cursor()
        cutoff = datetime.now() - timedelta(days=days)
        cursor.execute("DELETE FROM notifications WHERE created_at < ?", (cutoff,))
        deleted = cursor.rowcount
        self.conn.commit()
        return deleted

    def cleanup_old_stats(self, days=90):
        cursor = self.conn.cursor()
        cutoff = datetime.now() - timedelta(days=days)
        cursor.execute("DELETE FROM daily_stats WHERE date < ?", (cutoff.strftime('%Y-%m-%d'),))
        deleted = cursor.rowcount
        self.conn.commit()
        return deleted

    def export_config(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT keyword, group_name, enabled FROM keywords")
        keywords = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT name, url, enabled, check_interval FROM rss_sources")
        sources = [dict(row) for row in cursor.fetchall()]
        return {'keywords': keywords, 'sources': sources, 'export_time': datetime.now().isoformat()}

    def export_stats(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM daily_stats ORDER BY date DESC LIMIT 30")
        daily = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT keyword, match_count FROM keywords WHERE match_count > 0 ORDER BY match_count DESC")
        keywords = [dict(row) for row in cursor.fetchall()]
        return {'daily_stats': daily, 'keyword_stats': keywords, 'export_time': datetime.now().isoformat()}

    def reset_to_default(self):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM rss_sources")
        self.conn.commit()
        monitor_state.global_interval = 60
        monitor_state.global_timeout = 30
        monitor_state.keyword_notify = True
        monitor_state.system_notify = True
        monitor_state.error_notify = True
        return True


# ============= Telegram Bot =============
class TelegramBot:
    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.bot_token = config.get('telegram', {}).get('bot_token', '')
        self.chat_id = str(config.get('telegram', {}).get('chat_id', ''))
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

    def send_message(self, text, reply_markup=None, reply_to=None):
        if not self.bot_token or not self.chat_id:
            return False, None
        try:
            url = f"{self.base_url}/sendMessage"
            data = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
            if reply_to:
                data["reply_to_message_id"] = reply_to
            response = requests.post(url, data=data, timeout=10)
            if response.status_code == 200:
                result = response.json()
                return True, result.get('result', {}).get('message_id')
            return False, None
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return False, None

    def send_document(self, file_path, caption=None):
        if not self.bot_token or not self.chat_id:
            return False
        try:
            url = f"{self.base_url}/sendDocument"
            with open(file_path, 'rb') as f:
                files = {'document': f}
                data = {'chat_id': self.chat_id}
                if caption:
                    data['caption'] = caption
                response = requests.post(url, files=files, data=data, timeout=30)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"发送文件失败: {e}")
            return False

    def edit_message(self, text, message_id, reply_markup=None):
        try:
            url = f"{self.base_url}/editMessageText"
            data = {"chat_id": self.chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
            response = requests.post(url, data=data, timeout=10)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"编辑消息失败: {e}")
            return False

    def answer_callback(self, query_id, text=None, show_alert=False):
        try:
            url = f"{self.base_url}/answerCallbackQuery"
            data = {"callback_query_id": query_id}
            if text:
                data["text"] = text
                data["show_alert"] = show_alert
            requests.post(url, data=data, timeout=5)
            return True
        except:
            return False

    def delete_message(self, message_id):
        try:
            url = f"{self.base_url}/deleteMessage"
            data = {"chat_id": self.chat_id, "message_id": message_id}
            response = requests.post(url, data=data, timeout=5)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"删除消息失败: {e}")
            return False


# ============= RSS检查器 =============
class RSSChecker:
    def __init__(self, db, bot):
        self.db = db
        self.bot = bot

    def generate_unique_key(self, source_name, title, author, link):
        post_id = None
        patterns = [r'/post-(\d+)', r'/post/(\d+)', r'/topic/(\d+)', r'/thread/(\d+)', r'/(\d+)$']
        for pattern in patterns:
            match = re.search(pattern, link)
            if match:
                post_id = match.group(1)
                break
        if not post_id:
            post_id = hashlib.md5(link.encode()).hexdigest()[:8]
        author_clean = re.sub(r'[^\w\u4e00-\u9fff]', '', author).lower() if author else 'unknown'
        return f"{source_name}_{post_id}_{author_clean}"

    def should_notify_with_filters(self, title, source_text, keyword_id, keyword):
        """
        检查是否应该发送通知
        :param title: 原始标题，用于正则匹配
        :param source_text: 用于排除词检查的扩展文本（标题+摘要+标签）
        :param keyword_id: 关键词ID
        :param keyword: 关键词文本
        """
        filters = self.db.get_filters(keyword_id)

        exclude_words = []
        regex_patterns = []

        for f in filters:
            if f['filter_type'] == 'exclude':
                exclude_words.append(f['filter_value'])
            elif f['filter_type'] == 'regex':
                regex_patterns.append(f['filter_value'])

        # 排除词检查（在扩展文本上检查，范围更广）
        for word in exclude_words:
            if word.lower() in source_text:
                logger.info(f"关键词 '{keyword}' 匹配但被排除词 '{word}' 过滤")
                return False

        # 正则检查（仍然只在标题上检查，保持规则严谨）
        for pattern in regex_patterns:
            try:
                if not re.search(pattern, title, re.IGNORECASE):
                    logger.info(f"关键词 '{keyword}' 匹配但未通过正则 '{pattern}'")
                    return False
            except re.error:
                continue

        return True

    def is_duplicate_title(self, title):
        recent_titles = self.db.get_recent_titles(50)
        for recent in recent_titles:
            if is_similar_title(title, recent):
                logger.info(f"标题与已有推送相似，跳过: '{title[:50]}...' ≈ '{recent[:50]}...'")
                return True
        return False

    def check_source(self, source):
        source_id = source['id']
        source_name = source['name']
        url = source['url']
        timeout = source['timeout'] if 'timeout' in source.keys() else 30
        user_agent = source['user_agent'] if 'user_agent' in source.keys() and source['user_agent'] else DEFAULT_UA

        try:
            headers = {'User-Agent': user_agent}
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code != 200:
                self.db.update_source_status(source_id, f"HTTP {response.status_code}", True)
                return -1

            feed = feedparser.parse(response.content)
            if not feed.entries:
                self.db.update_source_status(source_id, "无条目", False)
                return 0

            keywords = self.db.get_all_keywords(enabled_only=True)
            if not keywords:
                self.db.update_source_status(source_id, "无关键词", False)
                return 0

            matches = 0
            for entry in feed.entries:
                title = entry.get('title', '')
                link = entry.get('link', '')
                author = entry.get('author', '未知')
                if not title or not link:
                    continue
                title = re.sub(r'<[^>]+>', '', title).strip()

                # ========== 新项目移植：构建 source_text 和提取版块 ==========
                # 1. 提取摘要
                summary = entry.get('summary', '')
                plain_summary = re.sub(r'<[^>]+>', ' ', summary)
                plain_summary = re.sub(r'\s+', ' ', plain_summary).strip()

                # 2. 提取标签
                tags = entry.get('tags', [])
                tag_terms = []
                for tag in tags:
                    if isinstance(tag, dict) and tag.get('term'):
                        tag_terms.append(tag.get('term'))

                # 3. 构建用于匹配的源文本
                source_parts = [title, plain_summary] + tag_terms
                source_text = ' '.join(part for part in source_parts if part).lower()

                # 4. 提取版块信息（为将来添加版块过滤做准备）
                category_slug = None
                for term in tag_terms:
                    category_slug = normalize_category_slug(term)
                    if category_slug:
                        break
                # ========== 移植结束 ==========

                # ========== 解析并修正时区 ==========
                created_at = None
                if hasattr(entry, 'published'):
                    try:
                        pubdate_utc = parsedate_to_datetime(entry.published)
                        pubdate_beijing = pubdate_utc + timedelta(hours=8)
                        created_at = pubdate_beijing.strftime('%Y-%m-%d %H:%M:%S')
                    except:
                        pass
                elif hasattr(entry, 'pubDate'):
                    try:
                        pubdate_utc = parsedate_to_datetime(entry.pubDate)
                        pubdate_beijing = pubdate_utc + timedelta(hours=8)
                        created_at = pubdate_beijing.strftime('%Y-%m-%d %H:%M:%S')
                    except:
                        pass
                # ========== 时区修正结束 ==========

                matched = []
                for kw_row in keywords:
                    kw = kw_row['keyword']
                    kw_id = kw_row['id']
                    # ⭐ 关键修改：在 source_text 中匹配，而不是 title
                    if kw.lower() in source_text:
                        if self.should_notify_with_filters(title, source_text, kw_id, kw):
                            matched.append(kw)

                if matched:
                    unique_key = self.generate_unique_key(source_name, title, author, link)
                    if self.db.is_notified(unique_key):
                        continue

                    # 去重检查
                    if self.is_duplicate_title(title):
                        content_preview = extract_content_preview(entry)
                        self.db.add_notification(unique_key, source_name, title, author, link, matched, content_preview, created_at)
                        continue

                    # 提取内容预览
                    content_preview = extract_content_preview(entry)
                    self.db.add_notification(unique_key, source_name, title, author, link, matched, content_preview, created_at)

                    if monitor_state.keyword_notify:
                        message = f"""🔔 <b>关键词匹配</b>

📌 <b>标题</b>: {title}
🔑 <b>关键词</b>: {', '.join(matched)}
👤 <b>作者</b>: {author}
📡 <b>来源</b>: {source_name}"""
                        if content_preview:
                            message += f"\n\n📄 <b>预览</b>:\n{content_preview}"
                        message += f"\n\n🔗 <b>链接</b>: {link}"
                        self.bot.send_message(message)
                    matches += 1
                    monitor_state.total_matches += 1

            self.db.update_source_status(source_id, f"成功，{matches}个匹配", False)
            return matches
        except requests.exceptions.Timeout:
            self.db.update_source_status(source_id, f"超时({timeout}s)", True)
            if monitor_state.error_notify:
                self.bot.send_message(f"⚠️ RSS源 {source_name} 检查超时 ({timeout}秒)")
            return -1
        except Exception as e:
            self.db.update_source_status(source_id, str(e)[:50], True)
            if monitor_state.error_notify:
                self.bot.send_message(f"⚠️ RSS源 {source_name} 检查失败: {e}")
            return -1

    def check_all_sources(self):
        sources = self.db.get_enabled_sources()
        if not sources:
            return 0

        if HEALTH_CHECK_ENABLED:
            disabled_count = self.db.check_and_disable_unhealthy_sources()
            if disabled_count > 0:
                logger.info(f"健康检查：已自动禁用 {disabled_count} 个不健康的源")
                if monitor_state.system_notify:
                    self.bot.send_message(f"⚠️ 健康检查：{disabled_count} 个源因连续失败已被自动禁用")

        total_matches = 0
        for source in sources:
            matches = self.check_source(dict(source))
            if matches > 0:
                total_matches += matches
            time.sleep(2)
        monitor_state.last_check = datetime.now()
        monitor_state.total_checks += 1
        self.db.record_daily_stats()
        return total_matches


# ============= 菜单系统 =============
class MenuManager:
    def __init__(self, bot, db):
        self.bot = bot
        self.db = db

    def get_main_menu(self):
        status = monitor_state.get_status_text()
        stats = self.db.get_stats()
        text = f"""<b>🤖 RSS监控中心 v4.0</b>

{status}
🔑 关键词: {stats['total_keywords']} 个  📡 RSS源: {stats['total_sources']} 个
📬 总推送: {stats['total_notifications']} 次  📅 今日: {stats['today_matches']} 次

请选择功能："""
        keyboard = {
            "inline_keyboard": [
                [{"text": "📋 关键词管理", "callback_data": "menu_keywords"},
                 {"text": "📊 统计中心", "callback_data": "menu_stats"}],
                [{"text": "📡 源管理", "callback_data": "menu_sources"},
                 {"text": "⚙️ 系统设置", "callback_data": "menu_settings"}],
                [{"text": "⏸ 暂停监控", "callback_data": "menu_pause"},
                 {"text": "▶️ 恢复监控", "callback_data": "menu_resume"}],
                [{"text": "🔄 立即检测", "callback_data": "check_now"},
                 {"text": "📈 实时状态", "callback_data": "menu_status"}],
                [{"text": "💡 帮助", "callback_data": "menu_help"},
                 {"text": "🔄 刷新", "callback_data": "refresh_main"}]
            ]
        }
        return text, keyboard

    def get_keywords_menu(self):
        groups = self.db.get_keywords_by_group()
        if not groups:
            text = "📋 <b>关键词管理</b>\n\n暂无关键词，请先添加。"
            keyboard = {
                "inline_keyboard": [
                    [{"text": "➕ 添加关键词", "callback_data": "add_keyword"}],
                    [{"text": "🔙 返回主菜单", "callback_data": "back_to_menu"}]
                ]
            }
        else:
            text = "📋 <b>关键词管理</b>\n\n点击关键词设置过滤规则："
            keyboard = {"inline_keyboard": []}
            for group, keywords in groups.items():
                text += f"\n<b>{group}</b>\n"
                for kw in keywords[:10]:
                    status = "🟢" if kw['enabled'] else "🔴"
                    filter_count = len(self.db.get_all_filters(kw['id']))
                    filter_badge = f" 🔍{filter_count}" if filter_count > 0 else ""
                    text += f"  {status} {kw['keyword']}{filter_badge}\n"
                    keyboard["inline_keyboard"].append([{
                        "text": f"🔍 过滤规则 - {kw['keyword']}",
                        "callback_data": f"filter_menu_{kw['id']}"
                    }])

            keyboard["inline_keyboard"].extend([
                [{"text": "➕ 添加关键词", "callback_data": "add_keyword"},
                 {"text": "🔛 切换状态", "callback_data": "toggle_keyword_menu"}],
                [{"text": "➖ 删除关键词", "callback_data": "delete_keyword_menu"}],
                [{"text": "🔙 返回主菜单", "callback_data": "back_to_menu"}]
            ])
        return text, keyboard

    def get_filter_menu(self, keyword_id, keyword):
        filters = self.db.get_all_filters(keyword_id)

        text = f"""🔍 <b>过滤规则管理</b>

关键词: <b>{keyword}</b>

<b>当前规则</b>"""

        if filters:
            for f in filters:
                ftype = "🚫 排除" if f['filter_type'] == 'exclude' else "📐 正则"
                status = "🟢" if f['enabled'] else "🔴"
                text += f"\n{status} {ftype}: {f['filter_value']}"
        else:
            text += "\n暂无过滤规则"

        text += "\n\n💡 排除词：标题包含该词则不推送"
        text += "\n💡 正则表达式：标题必须匹配才推送"

        keyboard = {
            "inline_keyboard": [
                [{"text": "🚫 添加排除词", "callback_data": f"add_exclude_{keyword_id}"}],
                [{"text": "📐 添加正则", "callback_data": f"add_regex_{keyword_id}"}],
            ]
        }
        if filters:
            keyboard["inline_keyboard"].append([
                {"text": "🔛 管理规则", "callback_data": f"manage_filters_{keyword_id}"}
            ])
        keyboard["inline_keyboard"].append([
            {"text": "🔙 返回", "callback_data": "menu_keywords"}
        ])
        return text, keyboard

    def get_manage_filters_menu(self, keyword_id, keyword):
        filters = self.db.get_all_filters(keyword_id)

        text = f"🔛 <b>管理过滤规则</b>\n\n关键词: {keyword}\n\n点击规则切换启用/禁用，或删除："

        keyboard = {"inline_keyboard": []}
        for f in filters:
            ftype = "🚫" if f['filter_type'] == 'exclude' else "📐"
            status = "🟢" if f['enabled'] else "🔴"
            display_value = f['filter_value'][:20] + "..." if len(f['filter_value']) > 20 else f['filter_value']
            keyboard["inline_keyboard"].append([{
                "text": f"{status} {ftype} {display_value}",
                "callback_data": f"toggle_filter_{f['id']}"
            }])
            keyboard["inline_keyboard"].append([{
                "text": f"   🗑 删除这条规则",
                "callback_data": f"delete_filter_{f['id']}"
            }])

        keyboard["inline_keyboard"].append([{"text": "🔙 返回", "callback_data": f"filter_menu_{keyword_id}"}])
        return text, keyboard

    def get_toggle_keyword_menu(self):
        keywords = self.db.get_all_keywords(enabled_only=False)
        if not keywords:
            return None, None
        text = "🔛 <b>切换关键词状态</b>\n\n点击关键词切换启用/禁用："
        keyboard = {"inline_keyboard": []}
        for kw in keywords[:20]:
            status = "🟢" if kw['enabled'] else "🔴"
            keyboard["inline_keyboard"].append([{
                "text": f"{status} {kw['keyword']}",
                "callback_data": f"toggle_kw_{kw['id']}"
            }])
        keyboard["inline_keyboard"].append([{"text": "🔙 返回", "callback_data": "menu_keywords"}])
        return text, keyboard

    def get_delete_keyword_menu(self):
        keywords = self.db.get_all_keywords(enabled_only=False)
        if not keywords:
            return None, None
        text = "➖ <b>选择要删除的关键词</b>"
        keyboard = {"inline_keyboard": []}
        for kw in keywords[:20]:
            keyboard["inline_keyboard"].append([{
                "text": f"❌ {kw['keyword']}",
                "callback_data": f"confirm_delete_kw_{kw['id']}"
            }])
        keyboard["inline_keyboard"].append([{"text": "🔙 返回", "callback_data": "menu_keywords"}])
        return text, keyboard

    def get_stats_menu(self):
        stats = self.db.get_stats()
        text = f"""📊 <b>统计中心</b>

━━━━━━━━━━━━━━━━━━━━
🔑 监控关键词: {stats['total_keywords']}
📡 启用RSS源: {stats['total_sources']}
📬 总推送次数: {stats['total_notifications']}
📅 今日匹配: {stats['today_matches']}
📆 本周匹配: {stats['week_matches']}

<b>🔥 热门关键词</b>"""
        if stats['top_keywords']:
            for kw in stats['top_keywords']:
                text += f"\n• {kw['keyword']}: {kw['match_count']} 次"
        else:
            text += "\n暂无数据"
        keyboard = {
            "inline_keyboard": [
                [{"text": "🔄 刷新统计", "callback_data": "menu_stats"}],
                [{"text": "🔙 返回主菜单", "callback_data": "back_to_menu"}]
            ]
        }
        return text, keyboard

    def get_sources_menu(self):
        sources = self.db.get_all_sources()
        text = "📡 <b>RSS源管理</b>\n\n"
        for src in sources:
            status = "🟢" if src['enabled'] else "🔴"
            last_check = src['last_check'] or "从未"
            if isinstance(last_check, str) and len(last_check) > 16:
                last_check = last_check[5:16]
            timeout = src['timeout'] if 'timeout' in src.keys() else 30
            consecutive = src['consecutive_errors'] if 'consecutive_errors' in src.keys() else 0
            health = ""
            if consecutive >= 3:
                health = " ⚠️"
            elif consecutive >= 5:
                health = " 🔴"
            text += f"{status} <b>{src['name']}</b>{health}\n"
            text += f"   └ 间隔: {src['check_interval']}s | 超时: {timeout}s | 连续失败: {consecutive}\n"
            text += f"   └ 上次: {last_check}\n\n"

        keyboard = {"inline_keyboard": []}
        row = []
        for src in sources[:6]:
            row.append({
                "text": f"{'🟢' if src['enabled'] else '🔴'} {src['name'][:8]}",
                "callback_data": f"toggle_source_{src['id']}"
            })
            if len(row) == 2:
                keyboard["inline_keyboard"].append(row)
                row = []
        if row:
            keyboard["inline_keyboard"].append(row)

        keyboard["inline_keyboard"].extend([
            [{"text": "➕ 添加RSS源", "callback_data": "add_source"}],
            [{"text": "🗑 删除RSS源", "callback_data": "delete_source_menu"}],
            [{"text": "🔙 返回主菜单", "callback_data": "back_to_menu"}]
        ])
        return text, keyboard

    def get_delete_source_menu(self):
        sources = self.db.get_all_sources()
        text = "🗑 <b>选择要删除的RSS源</b>"
        keyboard = {"inline_keyboard": []}
        for src in sources:
            keyboard["inline_keyboard"].append([{
                "text": f"❌ {src['name']}",
                "callback_data": f"delete_source_{src['id']}"
            }])
        keyboard["inline_keyboard"].append([{"text": "🔙 返回", "callback_data": "menu_sources"}])
        return text, keyboard

    def get_pause_menu(self):
        text = f"""⏸ <b>暂停监控</b>

当前状态: {monitor_state.get_status_text()}

选择暂停时长："""
        keyboard = {
            "inline_keyboard": [
                [{"text": "🕐 暂停 5 分钟", "callback_data": "pause_5"}],
                [{"text": "🕑 暂停 15 分钟", "callback_data": "pause_15"}],
                [{"text": "🕒 暂停 30 分钟", "callback_data": "pause_30"}],
                [{"text": "🕓 暂停 1 小时", "callback_data": "pause_60"}],
                [{"text": "🕔 暂停 2 小时", "callback_data": "pause_120"}],
                [{"text": "⏹ 手动暂停", "callback_data": "pause_manual"}],
                [{"text": "🔙 返回主菜单", "callback_data": "back_to_menu"}]
            ]
        }
        return text, keyboard

    def get_settings_menu(self):
        text = """⚙️ <b>系统设置</b>

请选择要配置的项目："""
        keyboard = {
            "inline_keyboard": [
                [{"text": "⏱ 检查间隔设置", "callback_data": "settings_interval"}],
                [{"text": "🔔 通知设置", "callback_data": "settings_notify"}],
                [{"text": "🧹 清理历史记录", "callback_data": "settings_cleanup"}],
                [{"text": "💾 备份数据", "callback_data": "settings_backup"}],
                [{"text": "🔄 恢复默认设置", "callback_data": "settings_reset"}],
                [{"text": "🔙 返回主菜单", "callback_data": "back_to_menu"}]
            ]
        }
        return text, keyboard

    def get_interval_settings_menu(self):
        sources = self.db.get_all_sources()
        text = f"""⏱ <b>检查间隔设置</b>

<b>全局间隔</b>: {monitor_state.global_interval} 秒
<b>全局超时</b>: {monitor_state.global_timeout} 秒

<b>各源间隔</b>"""
        for src in sources[:10]:
            timeout = src['timeout'] if 'timeout' in src.keys() else 30
            text += f"\n• {src['name']}: {src['check_interval']}s / {timeout}s"

        keyboard = {
            "inline_keyboard": [
                [{"text": "🌐 设置全局间隔", "callback_data": "set_global_interval"}],
                [{"text": "⏰ 设置全局超时", "callback_data": "set_global_timeout"}],
                [{"text": "🔙 返回设置", "callback_data": "menu_settings"}]
            ]
        }
        return text, keyboard

    def get_notify_settings_menu(self):
        text = f"""🔔 <b>通知设置</b>

<b>当前状态</b>
• 关键词通知: {'🟢 开启' if monitor_state.keyword_notify else '🔴 关闭'}
• 系统通知: {'🟢 开启' if monitor_state.system_notify else '🔴 关闭'}
• 错误告警: {'🟢 开启' if monitor_state.error_notify else '🔴 关闭'}

点击下方切换："""
        keyboard = {
            "inline_keyboard": [
                [{"text": f"{'🔔' if monitor_state.keyword_notify else '🔕'} 关键词通知", "callback_data": "toggle_keyword_notify"}],
                [{"text": f"{'📢' if monitor_state.system_notify else '🔕'} 系统通知", "callback_data": "toggle_system_notify"}],
                [{"text": f"{'⚠️' if monitor_state.error_notify else '🔕'} 错误告警", "callback_data": "toggle_error_notify"}],
                [{"text": "🔙 返回设置", "callback_data": "menu_settings"}]
            ]
        }
        return text, keyboard

    def get_cleanup_menu(self):
        counts = self.db.get_data_counts()
        text = f"""🧹 <b>清理历史记录</b>

<b>当前数据量</b>
• 通知记录: {counts['notifications']} 条
• 统计记录: {counts['stats']} 条

请选择清理选项："""
        keyboard = {
            "inline_keyboard": [
                [{"text": "🗑 清理7天前通知", "callback_data": "cleanup_7"}],
                [{"text": "🗑 清理30天前通知", "callback_data": "cleanup_30"}],
                [{"text": "🗑 清理90天前通知", "callback_data": "cleanup_90"}],
                [{"text": "📊 清理90天前统计", "callback_data": "cleanup_stats"}],
                [{"text": "🧹 清理所有历史", "callback_data": "cleanup_all"}],
                [{"text": "🔙 返回设置", "callback_data": "menu_settings"}]
            ]
        }
        return text, keyboard

    def get_backup_menu(self):
        text = """💾 <b>备份数据</b>

请选择备份方式："""
        keyboard = {
            "inline_keyboard": [
                [{"text": "📤 导出配置(JSON)", "callback_data": "backup_export_json"}],
                [{"text": "📊 导出统计数据", "callback_data": "backup_export_stats"}],
                [{"text": "🗄 备份完整数据库", "callback_data": "backup_database"}],
                [{"text": "📁 创建完整备份", "callback_data": "backup_full"}],
                [{"text": "🔙 返回设置", "callback_data": "menu_settings"}]
            ]
        }
        return text, keyboard

    def get_reset_confirm_menu(self):
        text = """⚠️ <b>恢复默认设置</b>

此操作将：
• 重置所有RSS源为默认
• 恢复默认检查间隔
• 恢复默认通知设置

<b>关键词和历史记录将保留</b>

确定要恢复默认设置吗？"""
        keyboard = {
            "inline_keyboard": [
                [{"text": "✅ 确认恢复", "callback_data": "confirm_reset"}],
                [{"text": "❌ 取消", "callback_data": "menu_settings"}]
            ]
        }
        return text, keyboard

    def get_status_menu(self):
        status_text = monitor_state.get_status_text()
        stats = self.db.get_stats()
        text = f"""📈 <b>实时状态</b>

{status_text}
━━━━━━━━━━━━━━━━━━━━
🔑 关键词: {stats['total_keywords']} 个
📡 RSS源: {stats['total_sources']} 个
📬 总推送: {stats['total_notifications']} 次
📅 今日: {stats['today_matches']} 次

⏱ 总检查: {monitor_state.total_checks} 次
📊 总匹配: {monitor_state.total_matches} 次
🕐 上次检查: {monitor_state.last_check.strftime('%H:%M:%S') if monitor_state.last_check else '从未'}"""
        keyboard = {
            "inline_keyboard": [
                [{"text": "🔄 刷新", "callback_data": "menu_status"}],
                [{"text": "🔙 返回", "callback_data": "back_to_menu"}]
            ]
        }
        return text, keyboard

    def get_help_menu(self):
        text = """💡 <b>使用帮助</b>

<b>📋 关键词管理</b>
• 添加/删除监控关键词
• 可切换启用/禁用状态
• 🔍 设置过滤规则

<b>📡 源管理</b>
• 添加/删除 RSS 源
• 自定义检查间隔和超时

<b>🛡️ 健康检查</b>
• 连续失败5次自动禁用

<b>🕐 时区修正</b>
• 抓取时自动转换 UTC+0 为北京时间

<b>💬 快捷指令</b>
点击下方按钮执行命令："""

        keyboard = {
            "inline_keyboard": [
                [{"text": "/start 打开主菜单", "callback_data": "cmd_start"}],
                [{"text": "/pause 暂停监控", "callback_data": "cmd_pause"}],
                [{"text": "/resume 恢复监控", "callback_data": "cmd_resume"}],
                [{"text": "/status 查看状态", "callback_data": "cmd_status"}],
                [{"text": "🔙 返回主菜单", "callback_data": "back_to_menu"}]
            ]
        }
        return text, keyboard


# ============= 回调处理器 =============
class CallbackHandler:
    def __init__(self, db, bot, menu_manager, checker):
        self.db = db
        self.bot = bot
        self.menu = menu_manager
        self.checker = checker

    def handle(self, callback_query):
        query_id = callback_query['id']
        data = callback_query['data']
        logger.info(f"收到回调查询: {data}")
        message = callback_query.get('message', {})
        chat_id = str(message.get('chat', {}).get('id'))
        message_id = message.get('message_id')

        self.bot.answer_callback(query_id)

        if data == "back_to_menu":
            text, keyboard = self.menu.get_main_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "refresh_main":
            text, keyboard = self.menu.get_main_menu()
            self.bot.edit_message(text, message_id, keyboard)
            self.bot.answer_callback(query_id, "已刷新", show_alert=False)

        elif data == "menu_keywords":
            text, keyboard = self.menu.get_keywords_menu()
            if text:
                self.bot.edit_message(text, message_id, keyboard)

        elif data == "add_keyword":
            user_states[chat_id] = {"state": "waiting_for_keyword", "message_id": message_id}
            text = "➕ <b>添加关键词</b>\n\n请直接发送要添加的关键词。\n\n💡 发送 /cancel 取消"
            keyboard = {"inline_keyboard": [[{"text": "❌ 取消", "callback_data": "menu_keywords"}]]}
            self.bot.edit_message(text, message_id, keyboard)
            self.bot.answer_callback(query_id, "请输入关键词", show_alert=True)

        elif data == "toggle_keyword_menu":
            text, keyboard = self.menu.get_toggle_keyword_menu()
            if text:
                self.bot.edit_message(text, message_id, keyboard)
            else:
                self.bot.answer_callback(query_id, "暂无关键词", show_alert=True)

        elif data.startswith("toggle_kw_"):
            kw_id = int(data.split("_")[2])
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT enabled, keyword FROM keywords WHERE id = ?", (kw_id,))
            row = cursor.fetchone()
            if row:
                new_state = not row['enabled']
                self.db.toggle_keyword(kw_id, new_state)
                self.bot.answer_callback(query_id, f"{row['keyword']} 已{'启用' if new_state else '禁用'}", show_alert=True)
            text, keyboard = self.menu.get_toggle_keyword_menu()
            if text:
                self.bot.edit_message(text, message_id, keyboard)

        elif data == "delete_keyword_menu":
            text, keyboard = self.menu.get_delete_keyword_menu()
            if text:
                self.bot.edit_message(text, message_id, keyboard)
            else:
                self.bot.answer_callback(query_id, "暂无关键词", show_alert=True)

        elif data.startswith("confirm_delete_kw_"):
            kw_id = int(data.split("_")[3])
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT keyword FROM keywords WHERE id = ?", (kw_id,))
            row = cursor.fetchone()
            if row:
                self.db.delete_keyword(row['keyword'])
                self.bot.answer_callback(query_id, f"已删除: {row['keyword']}", show_alert=True)
            text, keyboard = self.menu.get_delete_keyword_menu()
            if text:
                self.bot.edit_message(text, message_id, keyboard)
            else:
                text, keyboard = self.menu.get_keywords_menu()
                self.bot.edit_message(text, message_id, keyboard)

        elif data.startswith("filter_menu_"):
            kw_id = int(data.split("_")[2])
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT keyword FROM keywords WHERE id = ?", (kw_id,))
            row = cursor.fetchone()
            if row:
                text, keyboard = self.menu.get_filter_menu(kw_id, row['keyword'])
                self.bot.edit_message(text, message_id, keyboard)

        elif data.startswith("add_exclude_"):
            kw_id = int(data.split("_")[2])
            user_states[chat_id] = {"state": "waiting_for_exclude", "keyword_id": kw_id, "message_id": message_id}
            text = "🚫 <b>添加排除词</b>\n\n请输入要排除的词语：\n\n💡 标题包含该词则不推送"
            keyboard = {"inline_keyboard": [[{"text": "❌ 取消", "callback_data": f"filter_menu_{kw_id}"}]]}
            self.bot.edit_message(text, message_id, keyboard)
            self.bot.answer_callback(query_id, "请输入排除词", show_alert=True)

        elif data.startswith("add_regex_"):
            kw_id = int(data.split("_")[2])
            user_states[chat_id] = {"state": "waiting_for_regex", "keyword_id": kw_id, "message_id": message_id}
            text = "📐 <b>添加正则表达式</b>\n\n请输入正则表达式：\n\n示例：\n• \\$\\d+ - 匹配价格\n• \\d+% - 匹配百分比"
            keyboard = {"inline_keyboard": [[{"text": "❌ 取消", "callback_data": f"filter_menu_{kw_id}"}]]}
            self.bot.edit_message(text, message_id, keyboard)
            self.bot.answer_callback(query_id, "请输入正则表达式", show_alert=True)

        elif data.startswith("manage_filters_"):
            kw_id = int(data.split("_")[2])
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT keyword FROM keywords WHERE id = ?", (kw_id,))
            row = cursor.fetchone()
            if row:
                text, keyboard = self.menu.get_manage_filters_menu(kw_id, row['keyword'])
                self.bot.edit_message(text, message_id, keyboard)

        elif data.startswith("toggle_filter_"):
            filter_id = int(data.split("_")[2])
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT enabled, keyword_id FROM keyword_filters WHERE id = ?", (filter_id,))
            row = cursor.fetchone()
            if row:
                new_state = not row['enabled']
                self.db.toggle_filter(filter_id, new_state)
                self.bot.answer_callback(query_id, f"规则已{'启用' if new_state else '禁用'}", show_alert=True)
                cursor.execute("SELECT keyword FROM keywords WHERE id = ?", (row['keyword_id'],))
                kw_row = cursor.fetchone()
                text, keyboard = self.menu.get_manage_filters_menu(row['keyword_id'], kw_row['keyword'] if kw_row else "")
                self.bot.edit_message(text, message_id, keyboard)

        elif data.startswith("delete_filter_"):
            filter_id = int(data.split("_")[2])
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT keyword_id FROM keyword_filters WHERE id = ?", (filter_id,))
            row = cursor.fetchone()
            if row:
                self.db.delete_filter(filter_id)
                self.bot.answer_callback(query_id, "规则已删除", show_alert=True)
                cursor.execute("SELECT keyword FROM keywords WHERE id = ?", (row['keyword_id'],))
                kw_row = cursor.fetchone()
                text, keyboard = self.menu.get_manage_filters_menu(row['keyword_id'], kw_row['keyword'] if kw_row else "")
                self.bot.edit_message(text, message_id, keyboard)

        elif data == "menu_stats":
            text, keyboard = self.menu.get_stats_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "menu_sources":
            text, keyboard = self.menu.get_sources_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data.startswith("toggle_source_"):
            source_id = int(data.split("_")[2])
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT enabled, name FROM rss_sources WHERE id = ?", (source_id,))
            row = cursor.fetchone()
            if row:
                new_state = not row['enabled']
                self.db.toggle_source(source_id, new_state)
                self.bot.answer_callback(query_id, f"{row['name']} 已{'启用' if new_state else '禁用'}", show_alert=True)
            text, keyboard = self.menu.get_sources_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "add_source":
            user_states[chat_id] = {"state": "waiting_for_source", "message_id": message_id}
            text = "📡 <b>添加RSS源</b>\n\n请按格式发送：\n<code>名称|URL|间隔|超时</code>\n\n示例：\n<code>NodeSeek|https://rss.nodeseek.com/|60|30</code>"
            keyboard = {"inline_keyboard": [[{"text": "❌ 取消", "callback_data": "menu_sources"}]]}
            self.bot.edit_message(text, message_id, keyboard)
            self.bot.answer_callback(query_id, "请按格式输入", show_alert=True)

        elif data == "delete_source_menu":
            text, keyboard = self.menu.get_delete_source_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data.startswith("delete_source_"):
            source_id = int(data.split("_")[2])
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT name FROM rss_sources WHERE id = ?", (source_id,))
            row = cursor.fetchone()
            if row:
                self.db.delete_source(source_id)
                self.bot.answer_callback(query_id, f"已删除: {row['name']}", show_alert=True)
            text, keyboard = self.menu.get_sources_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "menu_pause":
            text, keyboard = self.menu.get_pause_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data.startswith("pause_"):
            duration_map = {"pause_5": 5, "pause_15": 15, "pause_30": 30, "pause_60": 60, "pause_120": 120}
            if data in duration_map:
                minutes = duration_map[data]
                success, msg = monitor_state.pause(f"定时暂停 {minutes} 分钟", minutes)
                self.db.add_pause_record("pause", f"定时暂停 {minutes} 分钟", minutes)
            elif data == "pause_manual":
                success, msg = monitor_state.pause("手动暂停")
                self.db.add_pause_record("pause", "手动暂停", None)
            self.bot.answer_callback(query_id, msg, show_alert=True)
            text, keyboard = self.menu.get_main_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "menu_resume":
            success, msg = monitor_state.resume()
            if success:
                self.db.add_pause_record("resume", "手动恢复", None)
            self.bot.answer_callback(query_id, msg, show_alert=True)
            text, keyboard = self.menu.get_main_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "menu_settings":
            text, keyboard = self.menu.get_settings_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "settings_interval":
            text, keyboard = self.menu.get_interval_settings_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "set_global_interval":
            user_states[chat_id] = {"state": "waiting_for_global_interval", "message_id": message_id}
            text = f"🌐 <b>设置全局检查间隔</b>\n\n当前: {monitor_state.global_interval} 秒\n\n请发送新间隔 (20-600)："
            keyboard = {"inline_keyboard": [[{"text": "❌ 取消", "callback_data": "settings_interval"}]]}
            self.bot.edit_message(text, message_id, keyboard)
            self.bot.answer_callback(query_id, "请输入间隔秒数", show_alert=True)

        elif data == "set_global_timeout":
            user_states[chat_id] = {"state": "waiting_for_global_timeout", "message_id": message_id}
            text = f"⏰ <b>设置全局超时时间</b>\n\n当前: {monitor_state.global_timeout} 秒\n\n请发送新超时 (10-120)："
            keyboard = {"inline_keyboard": [[{"text": "❌ 取消", "callback_data": "settings_interval"}]]}
            self.bot.edit_message(text, message_id, keyboard)
            self.bot.answer_callback(query_id, "请输入超时秒数", show_alert=True)

        elif data == "settings_notify":
            text, keyboard = self.menu.get_notify_settings_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "toggle_keyword_notify":
            monitor_state.keyword_notify = not monitor_state.keyword_notify
            self.bot.answer_callback(query_id, f"关键词通知已{'开启' if monitor_state.keyword_notify else '关闭'}", show_alert=True)
            text, keyboard = self.menu.get_notify_settings_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "toggle_system_notify":
            monitor_state.system_notify = not monitor_state.system_notify
            self.bot.answer_callback(query_id, f"系统通知已{'开启' if monitor_state.system_notify else '关闭'}", show_alert=True)
            text, keyboard = self.menu.get_notify_settings_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "toggle_error_notify":
            monitor_state.error_notify = not monitor_state.error_notify
            self.bot.answer_callback(query_id, f"错误告警已{'开启' if monitor_state.error_notify else '关闭'}", show_alert=True)
            text, keyboard = self.menu.get_notify_settings_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "settings_cleanup":
            text, keyboard = self.menu.get_cleanup_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data.startswith("cleanup_"):
            if data == "cleanup_7":
                deleted = self.db.cleanup_old_notifications(7)
                self.bot.answer_callback(query_id, f"已清理 {deleted} 条记录", show_alert=True)
            elif data == "cleanup_30":
                deleted = self.db.cleanup_old_notifications(30)
                self.bot.answer_callback(query_id, f"已清理 {deleted} 条记录", show_alert=True)
            elif data == "cleanup_90":
                deleted = self.db.cleanup_old_notifications(90)
                self.bot.answer_callback(query_id, f"已清理 {deleted} 条记录", show_alert=True)
            elif data == "cleanup_stats":
                deleted = self.db.cleanup_old_stats(90)
                self.bot.answer_callback(query_id, f"已清理 {deleted} 条统计", show_alert=True)
            elif data == "cleanup_all":
                del1 = self.db.cleanup_old_notifications(30)
                del2 = self.db.cleanup_old_stats(90)
                self.bot.answer_callback(query_id, f"已清理 {del1} 条通知和 {del2} 条统计", show_alert=True)
            text, keyboard = self.menu.get_cleanup_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "settings_backup":
            text, keyboard = self.menu.get_backup_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "backup_export_json":
            config = self.db.export_config()
            backup_file = os.path.join(BACKUP_DIR, f"config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            with open(backup_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            self.bot.send_document(backup_file, "📤 配置文件导出")
            self.bot.answer_callback(query_id, "配置文件已导出", show_alert=True)
            text, keyboard = self.menu.get_backup_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "backup_export_stats":
            stats = self.db.export_stats()
            backup_file = os.path.join(BACKUP_DIR, f"stats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            with open(backup_file, 'w', encoding='utf-8') as f:
                json.dump(stats, f, ensure_ascii=False, indent=2)
            self.bot.send_document(backup_file, "📊 统计数据导出")
            self.bot.answer_callback(query_id, "统计数据已导出", show_alert=True)
            text, keyboard = self.menu.get_backup_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "backup_database":
            backup_file = os.path.join(BACKUP_DIR, f"monitor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
            shutil.copy2(DB_FILE, backup_file)
            self.bot.send_document(backup_file, "🗄 数据库备份")
            self.bot.answer_callback(query_id, "数据库已备份", show_alert=True)
            text, keyboard = self.menu.get_backup_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "backup_full":
            config = self.db.export_config()
            config_file = os.path.join(BACKUP_DIR, f"config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)

            db_file = os.path.join(BACKUP_DIR, f"monitor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
            shutil.copy2(DB_FILE, db_file)

            import tarfile
            archive_file = os.path.join(BACKUP_DIR, f"full_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tar.gz")
            with tarfile.open(archive_file, "w:gz") as tar:
                tar.add(config_file, arcname=os.path.basename(config_file))
                tar.add(db_file, arcname=os.path.basename(db_file))

            self.bot.send_document(archive_file, "📁 完整备份")
            self.bot.answer_callback(query_id, "完整备份已创建", show_alert=True)

            os.remove(config_file)
            os.remove(db_file)

            text, keyboard = self.menu.get_backup_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "settings_reset":
            text, keyboard = self.menu.get_reset_confirm_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "confirm_reset":
            self.db.reset_to_default()
            self.bot.answer_callback(query_id, "已恢复默认设置", show_alert=True)
            text, keyboard = self.menu.get_main_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "menu_status":
            text, keyboard = self.menu.get_status_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "check_now":
            self.bot.answer_callback(query_id, "正在检测...", show_alert=False)
            self.bot.edit_message("🔄 <b>正在检测RSS源...</b>\n\n请稍候...", message_id)
            matches = self.checker.check_all_sources()
            if matches > 0:
                text = f"✅ <b>检测完成！</b>\n\n发现 {matches} 个新的关键词匹配，已发送通知。"
            else:
                text = "✅ <b>检测完成！</b>\n\n没有发现新的关键词匹配。"
            keyboard = {"inline_keyboard": [[{"text": "🔙 返回主菜单", "callback_data": "back_to_menu"}]]}
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "cmd_start":
            text, keyboard = self.menu.get_main_menu()
            self.bot.edit_message(text, message_id, keyboard)
            self.bot.answer_callback(query_id, "已打开主菜单", show_alert=True)

        elif data == "cmd_pause":
            text, keyboard = self.menu.get_pause_menu()
            self.bot.edit_message(text, message_id, keyboard)
            self.bot.answer_callback(query_id, "已打开暂停菜单", show_alert=True)

        elif data == "cmd_resume":
            success, msg = monitor_state.resume()
            if success:
                self.db.add_pause_record("resume", "快捷恢复", None)
            self.bot.answer_callback(query_id, msg, show_alert=True)
            text, keyboard = self.menu.get_main_menu()
            self.bot.edit_message(text, message_id, keyboard)

        elif data == "cmd_status":
            text, keyboard = self.menu.get_status_menu()
            self.bot.edit_message(text, message_id, keyboard)
            self.bot.answer_callback(query_id, "已打开状态", show_alert=True)

        elif data == "menu_help":
            text, keyboard = self.menu.get_help_menu()
            self.bot.edit_message(text, message_id, keyboard)


# ============= 文本消息处理 =============
def handle_text_message(message, db, bot, menu_manager):
    chat_id = str(message.get('chat', {}).get('id'))
    text = message.get('text', '').strip()
    message_id = message.get('message_id')

    logger.info(f"收到消息: {text}")

    if text == "/start" or text == "/menu":
        if chat_id in user_states:
            del user_states[chat_id]
        menu_text, keyboard = menu_manager.get_main_menu()
        bot.send_message(menu_text, keyboard)

    elif text == "/pause":
        menu_text, keyboard = menu_manager.get_pause_menu()
        bot.send_message(menu_text, keyboard)

    elif text == "/resume":
        success, msg = monitor_state.resume()
        if success:
            db.add_pause_record("resume", "命令恢复", None)
        send_and_auto_delete(bot, msg, reply_to=message_id)

    elif text == "/status":
        menu_text, keyboard = menu_manager.get_status_menu()
        bot.send_message(menu_text, keyboard)

    elif text == "/cancel":
        if chat_id in user_states:
            del user_states[chat_id]
            send_and_auto_delete(bot, "❌ 已取消操作", reply_to=message_id)

    elif chat_id in user_states:
        state_info = user_states[chat_id]
        state = state_info.get('state')
        original_msg_id = state_info.get('message_id')

        if state == "waiting_for_keyword":
            del user_states[chat_id]
            if text and not text.startswith('/'):
                success, msg = db.add_keyword(text)
                send_and_auto_delete(bot, msg, reply_to=message_id)
            else:
                send_and_auto_delete(bot, "❌ 无效的关键词", reply_to=message_id)
            menu_text, keyboard = menu_manager.get_keywords_menu()
            if original_msg_id:
                bot.edit_message(menu_text, original_msg_id, keyboard)

        elif state == "waiting_for_source":
            del user_states[chat_id]
            parts = text.split('|')
            if len(parts) >= 2:
                name = parts[0].strip()
                url = parts[1].strip()
                interval = int(parts[2].strip()) if len(parts) > 2 else 60
                timeout = int(parts[3].strip()) if len(parts) > 3 else 30
                success, msg = db.add_source(name, url, interval, timeout)
                send_and_auto_delete(bot, msg, reply_to=message_id)
            else:
                send_and_auto_delete(bot, "❌ 格式错误", reply_to=message_id)
            menu_text, keyboard = menu_manager.get_sources_menu()
            if original_msg_id:
                bot.edit_message(menu_text, original_msg_id, keyboard)

        elif state == "waiting_for_global_interval":
            del user_states[chat_id]
            try:
                interval = int(text)
                if 20 <= interval <= 600:
                    monitor_state.global_interval = interval
                    send_and_auto_delete(bot, f"✅ 全局间隔已设为 {interval} 秒", reply_to=message_id)
                else:
                    send_and_auto_delete(bot, "❌ 间隔必须在 20-600 秒之间", reply_to=message_id)
            except ValueError:
                send_and_auto_delete(bot, "❌ 请输入有效的数字", reply_to=message_id)
            menu_text, keyboard = menu_manager.get_interval_settings_menu()
            if original_msg_id:
                bot.edit_message(menu_text, original_msg_id, keyboard)

        elif state == "waiting_for_global_timeout":
            del user_states[chat_id]
            try:
                timeout = int(text)
                if 10 <= timeout <= 120:
                    monitor_state.global_timeout = timeout
                    send_and_auto_delete(bot, f"✅ 全局超时已设为 {timeout} 秒", reply_to=message_id)
                else:
                    send_and_auto_delete(bot, "❌ 超时必须在 10-120 秒之间", reply_to=message_id)
            except ValueError:
                send_and_auto_delete(bot, "❌ 请输入有效的数字", reply_to=message_id)
            menu_text, keyboard = menu_manager.get_interval_settings_menu()
            if original_msg_id:
                bot.edit_message(menu_text, original_msg_id, keyboard)

        elif state == "waiting_for_exclude":
            kw_id = state_info.get('keyword_id')
            del user_states[chat_id]
            if text and not text.startswith('/'):
                success, msg = db.add_filter(kw_id, 'exclude', text)
                send_and_auto_delete(bot, msg, reply_to=message_id)
            else:
                send_and_auto_delete(bot, "❌ 无效的排除词", reply_to=message_id)
            cursor = db.conn.cursor()
            cursor.execute("SELECT keyword FROM keywords WHERE id = ?", (kw_id,))
            row = cursor.fetchone()
            menu_text, keyboard = menu_manager.get_filter_menu(kw_id, row['keyword'] if row else "")
            if original_msg_id:
                bot.edit_message(menu_text, original_msg_id, keyboard)

        elif state == "waiting_for_regex":
            kw_id = state_info.get('keyword_id')
            del user_states[chat_id]
            if text and not text.startswith('/'):
                try:
                    re.compile(text)
                    success, msg = db.add_filter(kw_id, 'regex', text)
                    send_and_auto_delete(bot, msg, reply_to=message_id)
                except re.error:
                    send_and_auto_delete(bot, "❌ 无效的正则表达式", reply_to=message_id)
            else:
                send_and_auto_delete(bot, "❌ 无效的正则表达式", reply_to=message_id)
            cursor = db.conn.cursor()
            cursor.execute("SELECT keyword FROM keywords WHERE id = ?", (kw_id,))
            row = cursor.fetchone()
            menu_text, keyboard = menu_manager.get_filter_menu(kw_id, row['keyword'] if row else "")
            if original_msg_id:
                bot.edit_message(menu_text, original_msg_id, keyboard)


# ============= 监控主循环 =============
def monitor_loop(db, bot, checker):
    logger.info("🚀 开始RSS监控 v4.0")
    if monitor_state.system_notify:
        keyboard = {"inline_keyboard": [[{"text": "📱 打开主菜单", "callback_data": "cmd_start"}]]}
        bot.send_message("🟢 <b>RSS监控 v4.0 智能匹配增强版 已启动</b>", reply_markup=keyboard)

    while monitor_state.running:
        try:
            if monitor_state.paused:
                if monitor_state.resume_time and datetime.now() >= monitor_state.resume_time:
                    monitor_state.resume()
                    db.add_pause_record("resume", "自动恢复", None)
                    if monitor_state.system_notify:
                        keyboard = {"inline_keyboard": [[{"text": "📱 打开主菜单", "callback_data": "cmd_start"}]]}
                        bot.send_message("🟢 <b>监控已自动恢复运行</b>", reply_markup=keyboard)
                time.sleep(5)
                continue

            checker.check_all_sources()

            sources = db.get_enabled_sources()
            avg_interval = sum([s['check_interval'] for s in sources]) / len(sources) if sources else monitor_state.global_interval
            check_interval = max(20, min(600, avg_interval + random.uniform(-10, 10)))

            sleep_time = 0
            while sleep_time < check_interval and monitor_state.running and not monitor_state.paused:
                time.sleep(1)
                sleep_time += 1

        except Exception as e:
            logger.error(f"监控循环异常: {e}")
            time.sleep(60)

    logger.info("监控已停止")


# ============= Telegram监听 =============
def telegram_listener(db, bot, menu_manager, checker):
    callback_handler = CallbackHandler(db, bot, menu_manager, checker)
    offset = 0
    logger.info("📡 Telegram监听已启动")

    while monitor_state.running:
        try:
            url = f"{bot.base_url}/getUpdates"
            response = requests.get(url, params={"timeout": 30, "offset": offset}, timeout=35)
            if response.status_code == 200:
                data = response.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        if "callback_query" in update:
                            callback = update["callback_query"]
                            if str(callback.get('message', {}).get('chat', {}).get('id')) == bot.chat_id:
                                callback_handler.handle(callback)
                        elif "message" in update:
                            message = update["message"]
                            if str(message.get('chat', {}).get('id')) == bot.chat_id:
                                handle_text_message(message, db, bot, menu_manager)
            time.sleep(1)
        except Exception as e:
            logger.error(f"Telegram监听异常: {e}")
            time.sleep(5)


# ============= 主函数 =============
def main():
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))

    bot_token = os.environ.get('TG_BOT_TOKEN', '').strip()
    chat_id = os.environ.get('TG_CHAT_ID', '').strip()

    if not bot_token or not chat_id:
        logger.error("❌ 请设置 TG_BOT_TOKEN 和 TG_CHAT_ID")
        sys.exit(1)

    db = Database()
    config = {'telegram': {'bot_token': bot_token, 'chat_id': chat_id}}
    bot = TelegramBot(config, db)
    menu_manager = MenuManager(bot, db)
    checker = RSSChecker(db, bot)

    tg_thread = Thread(target=telegram_listener, args=(db, bot, menu_manager, checker))
    tg_thread.daemon = True
    tg_thread.start()

    try:
        monitor_loop(db, bot, checker)
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    finally:
        monitor_state.running = False
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)


if __name__ == "__main__":
    main()
