#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS监控 Web 管理界面 v3.7
端口: 5000
新增：分页、导入/导出、健康状态
"""

import os
import sqlite3
import requests
import feedparser
import json
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, jsonify, request, redirect, url_for, session

app = Flask(__name__)
app.secret_key = os.environ.get('WEB_SECRET', 'rss_monitor_secret_key_2024')
WEB_PASSWORD = os.environ.get('WEB_PASSWORD', 'admin123')

DATA_DIR = '/data'
DB_FILE = os.path.join(DATA_DIR, 'monitor.db')
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')

# 默认 User-Agent
DEFAULT_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# ============= 登录验证装饰器 =============
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function

# ============= 数据库连接 =============
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

# ============= 登录页面 =============
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == WEB_PASSWORD:
            session['logged_in'] = True
            return redirect('/')
        else:
            return render_template('login.html', error='密码错误')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect('/login')

# ============= 页面路由 =============
@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/keywords')
@login_required
def keywords_page():
    return render_template('keywords.html')

@app.route('/sources')
@login_required
def sources_page():
    return render_template('sources.html')

@app.route('/stats')
@login_required
def stats_page():
    return render_template('stats.html')

# ============= API 接口 =============
@app.route('/api/stats')
@login_required
def api_stats():
    """获取统计数据"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM keywords WHERE enabled = 1")
    total_keywords = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM rss_sources WHERE enabled = 1")
    total_sources = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM notifications")
    total_notifications = cursor.fetchone()[0]
    
    today = datetime.now().strftime('%Y-%m-%d')
    cursor.execute("SELECT COUNT(*) FROM notifications WHERE date(created_at) = ?", (today,))
    today_matches = cursor.fetchone()[0]
    
    conn.close()
    
    return jsonify({
        'total_keywords': total_keywords,
        'total_sources': total_sources,
        'total_notifications': total_notifications,
        'today_matches': today_matches
    })

# ============= 关键词 API（支持分页 + 导入/导出）=============
@app.route('/api/keywords', methods=['GET', 'POST'])
@login_required
def api_keywords():
    """关键词列表 API（支持分页）"""
    conn = get_db()
    cursor = conn.cursor()
    
    if request.method == 'GET':
        page = request.args.get('page', 1, type=int)
        page_size = request.args.get('page_size', 10, type=int)
        offset = (page - 1) * page_size
        
        # 获取总数
        cursor.execute("SELECT COUNT(*) FROM keywords")
        total = cursor.fetchone()[0]
        
        # 获取分页数据
        cursor.execute('''
            SELECT id, keyword, group_name, enabled, match_count 
            FROM keywords 
            ORDER BY enabled DESC, created_at DESC
            LIMIT ? OFFSET ?
        ''', (page_size, offset))
        keywords = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        return jsonify({
            'keywords': keywords,
            'total': total,
            'page': page,
            'page_size': page_size,
            'total_pages': (total + page_size - 1) // page_size
        })
    
    elif request.method == 'POST':
        data = request.json
        keyword = data.get('keyword', '').strip()
        group = data.get('group', '默认')
        
        if not keyword:
            return jsonify({'success': False, 'error': '关键词不能为空'})
        
        try:
            cursor.execute(
                "INSERT INTO keywords (keyword, group_name) VALUES (?, ?)",
                (keyword, group)
            )
            conn.commit()
            return jsonify({'success': True, 'id': cursor.lastrowid})
        except sqlite3.IntegrityError:
            return jsonify({'success': False, 'error': '关键词已存在'})
        finally:
            conn.close()

@app.route('/api/keywords/export', methods=['GET'])
@login_required
def api_export_keywords():
    """导出关键词"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT keyword, group_name, enabled, match_count 
        FROM keywords 
        ORDER BY group_name, keyword
    ''')
    keywords = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(keywords)

@app.route('/api/keywords/import', methods=['POST'])
@login_required
def api_import_keywords():
    """批量导入关键词"""
    data = request.json
    keywords_data = data.get('keywords', [])
    
    conn = get_db()
    cursor = conn.cursor()
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
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'added': added, 'skipped': skipped})

@app.route('/api/keywords/<int:kw_id>', methods=['PUT', 'DELETE'])
@login_required
def api_keyword(kw_id):
    """单个关键词操作"""
    conn = get_db()
    cursor = conn.cursor()
    
    if request.method == 'PUT':
        data = request.json
        enabled = data.get('enabled', 1)
        cursor.execute("UPDATE keywords SET enabled = ? WHERE id = ?", (enabled, kw_id))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    
    elif request.method == 'DELETE':
        cursor.execute("DELETE FROM keywords WHERE id = ?", (kw_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

# ============= 过滤规则 API =============
@app.route('/api/keywords/<int:kw_id>/filters', methods=['GET'])
@login_required
def api_keyword_filters(kw_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, filter_type, filter_value, enabled 
        FROM keyword_filters 
        WHERE keyword_id = ?
        ORDER BY filter_type, created_at
    ''', (kw_id,))
    filters = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(filters)

@app.route('/api/keywords/<int:kw_id>/filters', methods=['POST'])
@login_required
def api_add_filter(kw_id):
    data = request.json
    filter_type = data.get('type')
    filter_value = data.get('value', '').strip()
    
    if not filter_value:
        return jsonify({'success': False, 'error': '规则不能为空'})
    
    if filter_type not in ['exclude', 'regex']:
        return jsonify({'success': False, 'error': '无效的规则类型'})
    
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO keyword_filters (keyword_id, filter_type, filter_value)
            VALUES (?, ?, ?)
        ''', (kw_id, filter_type, filter_value))
        conn.commit()
        return jsonify({'success': True, 'id': cursor.lastrowid})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': '规则已存在'})
    finally:
        conn.close()

@app.route('/api/filters/<int:filter_id>', methods=['PUT', 'DELETE'])
@login_required
def api_filter(filter_id):
    conn = get_db()
    cursor = conn.cursor()
    
    if request.method == 'PUT':
        data = request.json
        enabled = data.get('enabled', 1)
        cursor.execute("UPDATE keyword_filters SET enabled = ? WHERE id = ?", (enabled, filter_id))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    
    elif request.method == 'DELETE':
        cursor.execute("DELETE FROM keyword_filters WHERE id = ?", (filter_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

# ============= 热门关键词 API（支持分页）=============
@app.route('/api/top-keywords')
@login_required
def api_top_keywords():
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 5, type=int)
    offset = (page - 1) * page_size
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT COUNT(*) FROM keywords 
        WHERE enabled = 1 AND match_count > 0
    ''')
    total = cursor.fetchone()[0]
    
    cursor.execute('''
        SELECT keyword, match_count FROM keywords 
        WHERE enabled = 1 AND match_count > 0
        ORDER BY match_count DESC
        LIMIT ? OFFSET ?
    ''', (page_size, offset))
    keywords = [dict(row) for row in cursor.fetchall()]
    
    cursor.execute("SELECT COUNT(*) FROM notifications")
    total_notifications = cursor.fetchone()[0]
    
    conn.close()
    
    return jsonify({
        'keywords': keywords,
        'total': total,
        'total_notifications': total_notifications,
        'page': page,
        'page_size': page_size,
        'total_pages': (total + page_size - 1) // page_size if total > 0 else 1
    })

# ============= RSS源 API（支持分页）=============
@app.route('/api/sources', methods=['GET', 'POST'])
@login_required
def api_sources():
    conn = get_db()
    cursor = conn.cursor()
    
    if request.method == 'GET':
        page = request.args.get('page', 1, type=int)
        page_size = request.args.get('page_size', 10, type=int)
        offset = (page - 1) * page_size
        
        cursor.execute("SELECT COUNT(*) FROM rss_sources")
        total = cursor.fetchone()[0]
        
        cursor.execute('''
            SELECT id, name, url, enabled, check_interval, 
                   COALESCE(timeout, 30) as timeout,
                   COALESCE(consecutive_errors, 0) as consecutive_errors,
                   last_check, last_status
            FROM rss_sources 
            ORDER BY enabled DESC, name
            LIMIT ? OFFSET ?
        ''', (page_size, offset))
        sources = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        return jsonify({
            'sources': sources,
            'total': total,
            'page': page,
            'page_size': page_size,
            'total_pages': (total + page_size - 1) // page_size if total > 0 else 1
        })
    
    elif request.method == 'POST':
        data = request.json
        name = data.get('name', '').strip()
        url = data.get('url', '').strip()
        interval = data.get('interval', 60)
        timeout = data.get('timeout', 30)
        user_agent = data.get('user_agent', '').strip() or DEFAULT_UA
        
        if not name or not url:
            return jsonify({'success': False, 'error': '名称和URL不能为空'})
        
        try:
            cursor.execute('''
                INSERT INTO rss_sources (name, url, check_interval, timeout, user_agent, enabled)
                VALUES (?, ?, ?, ?, ?, 1)
            ''', (name, url, interval, timeout, user_agent))
            conn.commit()
            return jsonify({'success': True, 'id': cursor.lastrowid})
        except sqlite3.IntegrityError:
            return jsonify({'success': False, 'error': 'RSS源名称已存在'})
        finally:
            conn.close()

@app.route('/api/sources/<int:src_id>', methods=['PUT', 'DELETE'])
@login_required
def api_source(src_id):
    conn = get_db()
    cursor = conn.cursor()
    
    if request.method == 'PUT':
        data = request.json
        name = data.get('name', '').strip()
        url = data.get('url', '').strip()
        interval = data.get('interval', 60)
        timeout = data.get('timeout', 30)
        user_agent = data.get('user_agent', '').strip() or DEFAULT_UA
        enabled = data.get('enabled', 1)
        
        if not name or not url:
            return jsonify({'success': False, 'error': '名称和URL不能为空'})
        
        try:
            cursor.execute('''
                UPDATE rss_sources 
                SET name = ?, url = ?, check_interval = ?, timeout = ?, user_agent = ?, enabled = ?
                WHERE id = ?
            ''', (name, url, interval, timeout, user_agent, enabled, src_id))
            conn.commit()
            return jsonify({'success': True})
        except sqlite3.IntegrityError:
            return jsonify({'success': False, 'error': 'RSS源名称已存在'})
        finally:
            conn.close()
    
    elif request.method == 'DELETE':
        cursor.execute("DELETE FROM rss_sources WHERE id = ?", (src_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

@app.route('/api/sources/<int:src_id>/toggle', methods=['POST'])
@login_required
def api_toggle_source(src_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE rss_sources SET enabled = 1 - enabled WHERE id = ?", (src_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/sources/<int:src_id>/check', methods=['POST'])
@login_required
def api_check_source(src_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, name, url, check_interval, 
               COALESCE(timeout, 30) as timeout,
               COALESCE(user_agent, ?) as user_agent
        FROM rss_sources WHERE id = ?
    ''', (DEFAULT_UA, src_id))
    source = cursor.fetchone()
    conn.close()
    
    if not source:
        return jsonify({'success': False, 'error': '源不存在'})
    
    try:
        headers = {'User-Agent': source['user_agent']}
        response = requests.get(source['url'], headers=headers, timeout=source['timeout'])
        
        if response.status_code != 200:
            return jsonify({'success': False, 'error': f'HTTP {response.status_code}'})
        
        feed = feedparser.parse(response.content)
        entries_count = len(feed.entries)
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE rss_sources 
            SET last_check = ?, last_status = ?, error_count = 0, consecutive_errors = 0
            WHERE id = ?
        ''', (datetime.now(), f'成功，{entries_count}条', src_id))
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'entries': entries_count,
            'message': f'检测成功，获取到 {entries_count} 条内容'
        })
        
    except requests.exceptions.Timeout:
        return jsonify({'success': False, 'error': f'超时 ({source["timeout"]}秒)'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)[:100]})

# ============= 源状态 API（支持分页）=============
@app.route('/api/sources-status')
@login_required
def api_sources_status():
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 5, type=int)
    offset = (page - 1) * page_size
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM rss_sources")
    total = cursor.fetchone()[0]
    
    cursor.execute('''
        SELECT id, name, enabled, 
               COALESCE(consecutive_errors, 0) as consecutive_errors,
               last_check, last_status
        FROM rss_sources 
        ORDER BY enabled DESC, name
        LIMIT ? OFFSET ?
    ''', (page_size, offset))
    sources = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return jsonify({
        'sources': sources,
        'total': total,
        'page': page,
        'page_size': page_size,
        'total_pages': (total + page_size - 1) // page_size if total > 0 else 1
    })

# ============= 最近推送 API（支持分页）=============
@app.route('/api/recent-notifications')
@login_required
def api_recent_notifications():
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)
    offset = (page - 1) * page_size
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM notifications")
    total = cursor.fetchone()[0]
    
    cursor.execute('''
        SELECT source_name, title, link, matched_keywords, content_preview, created_at 
        FROM notifications 
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    ''', (page_size, offset))
    notifications = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return jsonify({
        'notifications': notifications,
        'total': total,
        'page': page,
        'page_size': page_size,
        'total_pages': (total + page_size - 1) // page_size if total > 0 else 1
    })

# ============= 数据库升级 =============
def upgrade_database():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(rss_sources)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'user_agent' not in columns:
        cursor.execute("ALTER TABLE rss_sources ADD COLUMN user_agent TEXT")
    if 'consecutive_errors' not in columns:
        cursor.execute("ALTER TABLE rss_sources ADD COLUMN consecutive_errors INTEGER DEFAULT 0")
    cursor.execute("PRAGMA table_info(notifications)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'content_preview' not in columns:
        cursor.execute("ALTER TABLE notifications ADD COLUMN content_preview TEXT")
    conn.commit()
    conn.close()

# ============= 启动 =============
if __name__ == '__main__':
    upgrade_database()
    app.run(host='0.0.0.0', port=5000, debug=False)