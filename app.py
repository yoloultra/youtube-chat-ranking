import os
import json
import time
import threading
import sqlite3
import random
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string
from flask_cors import CORS
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import re
from threading import Lock
from queue import Queue
import traceback
import shutil
import socket

# Flask 앱 생성
app = Flask(__name__)
CORS(app)

# YouTube API 설정
YOUTUBE_API_SERVICE_NAME = 'youtube'
YOUTUBE_API_VERSION = 'v3'

# API 키 설정
API_KEY = 'AIzaSyDyC-5T5ePSqQmnfW9OFKXTdcuFbCqmgeI'

# 전역 변수
is_tracking = False
tracking_thread = None
live_chat_id = None
next_page_token = None
data_lock = Lock()
message_queue = Queue()
db_thread = None

# 데이터베이스 초기화
def init_database():
    """데이터베이스 초기화"""
    try:
        conn = sqlite3.connect('youtube_chat.db', timeout=30.0)
        conn.execute('PRAGMA journal_mode=WAL')
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                message TEXT NOT NULL,
                timestamp REAL NOT NULL,
                video_id TEXT,
                message_type TEXT DEFAULT 'counted',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_stats (
                username TEXT PRIMARY KEY,
                message_count INTEGER DEFAULT 0,
                last_message_time REAL,
                first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_info (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_messages_type ON messages(message_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_stats_count ON user_stats(message_count DESC)')
        
        conn.commit()
        conn.close()
        print("✅ 데이터베이스 초기화 완료")
    except Exception as e:
        print(f"❌ 데이터베이스 초기화 오류: {e}")
        traceback.print_exc()

# 데이터베이스 작업 큐 처리
def db_worker():
    """데이터베이스 작업을 별도 스레드에서 처리"""
    conn = sqlite3.connect('youtube_chat.db', timeout=30.0)
    conn.execute('PRAGMA journal_mode=WAL')
    
    while True:
        try:
            item = message_queue.get()
            if item is None:
                break
                
            username, message, timestamp, video_id, message_type = item
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO messages (username, message, timestamp, video_id, message_type)
                VALUES (?, ?, ?, ?, ?)
            ''', (username, message, timestamp, video_id, message_type))
            
            if message_type == 'counted':
                cursor.execute('''
                    INSERT OR REPLACE INTO user_stats (username, message_count, last_message_time, updated_at)
                    VALUES (?, 
                        COALESCE((SELECT message_count FROM user_stats WHERE username = ?), 0) + 1,
                        ?, 
                        CURRENT_TIMESTAMP)
                ''', (username, username, timestamp))
            
            conn.commit()
            
        except Exception as e:
            print(f"❌ DB 작업 오류: {e}")
            conn.rollback()
            
    conn.close()

class YouTubeChatTracker:
    def __init__(self):
        self.service = None
        self.authenticated = False
        self.retry_count = 0
        self.max_retries = 3
        
    def authenticate(self):
        """API 키만 사용하는 간단한 인증"""
        try:
            self.service = build(
                YOUTUBE_API_SERVICE_NAME, 
                YOUTUBE_API_VERSION, 
                developerKey=API_KEY
            )
            # API 테스트 요청
            test_request = self.service.videos().list(
                part="snippet",
                chart="mostPopular",
                maxResults=1
            )
            test_response = test_request.execute()
            
            self.authenticated = True
            print("✅ YouTube API 키 인증 성공!")
            return True
        except HttpError as e:
            print(f"❌ API 오류: {e}")
            if e.resp.status == 403:
                print("API 키가 유효하지 않거나 YouTube Data API v3가 활성화되지 않았습니다.")
            return False
        except Exception as e:
            print(f"❌ 서비스 생성 실패: {e}")
            return False
    
    def get_live_chat_id(self, video_id):
        """라이브 채팅 ID 가져오기"""
        if not self.service:
            print("❌ 서비스가 초기화되지 않았습니다.")
            return None
            
        try:
            request = self.service.videos().list(
                part="liveStreamingDetails,snippet",
                id=video_id
            )
            response = request.execute()
            
            if 'items' in response and len(response['items']) > 0:
                item = response['items'][0]
                if 'liveStreamingDetails' in item:
                    chat_id = item['liveStreamingDetails'].get('activeLiveChatId')
                    if chat_id:
                        title = item.get('snippet', {}).get('title', 'Unknown')
                        print(f"✅ 라이브 채팅 찾음: {title}")
                        return chat_id
                    else:
                        print("❌ 라이브 채팅이 활성화되지 않았습니다.")
                else:
                    print("❌ 라이브 스트리밍이 아닙니다.")
            else:
                print("❌ 비디오를 찾을 수 없습니다.")
            
            return None
        except HttpError as e:
            print(f"❌ HTTP 오류: {e}")
            return None
        except Exception as e:
            print(f"❌ 오류: {e}")
            traceback.print_exc()
            return None
    
    def get_chat_messages(self, live_chat_id, page_token=None):
        """채팅 메시지 가져오기"""
        if not self.service:
            return None
            
        try:
            params = {
                'liveChatId': live_chat_id,
                'part': 'snippet,authorDetails',
                'maxResults': 200
            }
            
            if page_token:
                params['pageToken'] = page_token
            
            request = self.service.liveChatMessages().list(**params)
            response = request.execute()
            self.retry_count = 0
            return response
        except Exception as e:
            print(f"❌ 채팅 메시지 조회 오류: {e}")
            self.retry_count += 1
            return None

# 트래커 인스턴스
chat_tracker = YouTubeChatTracker()

def is_spam_message(text):
    """스팸 메시지 필터링"""
    try:
        patterns = [
            r'(.)\1{4,}',
            r'^(.{1,3})\1{3,}$',
            r'[!@#$%^&*]{3,}',
            r'http[s]?://',
        ]
        return any(re.search(pattern, text) for pattern in patterns)
    except:
        return False

def is_emoji_only(text):
    """이모지만 있는 메시지 필터링"""
    try:
        basic_pattern = r'^[\s!@#$%^&*()ㅋㅎㅠㅜㄷㄱㅡ,.?]+$'
        if re.match(basic_pattern, text):
            return True
        
        if re.search(r'[a-zA-Z가-힣]', text):
            return False
            
        if re.match(r'^\d+$', text):
            return False
            
        return True
    except:
        return False

def parse_timestamp(published):
    """타임스탬프 파싱"""
    try:
        if hasattr(datetime, 'fromisoformat'):
            return datetime.fromisoformat(published.replace('Z', '+00:00')).timestamp()
        else:
            from dateutil import parser
            return parser.parse(published).timestamp()
    except:
        return time.time()

def process_message(username, message, timestamp, video_id):
    """메시지 처리 및 큐에 추가"""
    try:
        if len(message) < 2 or is_spam_message(message) or is_emoji_only(message):
            message_queue.put((username, message, timestamp, video_id, 'filtered'))
            return False
        
        message_queue.put((username, message, timestamp, video_id, 'counted'))
        print(f"💬 {username}: {message[:30]}{'...' if len(message) > 30 else ''}")
        return True
    except Exception as e:
        print(f"❌ 메시지 처리 오류: {e}")
        return False

def chat_polling_worker(video_id):
    """채팅 폴링 워커"""
    global is_tracking, live_chat_id, next_page_token
    
    try:
        # 새로운 추적 시작 시 상태 초기화
        next_page_token = None
        live_chat_id = None
        
        live_chat_id = chat_tracker.get_live_chat_id(video_id)
        if not live_chat_id:
            is_tracking = False
            return
        
        print("🚀 채팅 수집 시작!")
        error_count = 0
        
        # 처음 시작할 때는 이전 메시지를 건너뛰기 위해 첫 응답은 무시
        first_request = True
        
        while is_tracking:
            response = chat_tracker.get_chat_messages(live_chat_id, next_page_token)
            
            if not response:
                error_count += 1
                if error_count > 5:
                    print("❌ 연속 오류로 인해 추적을 중지합니다.")
                    break
                time.sleep(5)
                continue
            
            error_count = 0
            
            # 첫 번째 요청은 건너뛰기 (이전 메시지 무시)
            if first_request:
                first_request = False
                next_page_token = response.get('nextPageToken')
                print("📝 이전 메시지를 건너뛰고 새로운 메시지만 수집합니다.")
                time.sleep(2)
                continue
            
            count = 0
            for item in response.get('items', []):
                if not is_tracking:
                    break
                
                snippet = item.get('snippet', {})
                author = item.get('authorDetails', {})
                
                message = snippet.get('displayMessage', '')
                username = author.get('displayName', 'Unknown')
                published = snippet.get('publishedAt', '')
                
                timestamp = parse_timestamp(published)
                
                if process_message(username, message, timestamp, video_id):
                    count += 1
            
            if count > 0:
                print(f"📊 새 메시지 {count}개 처리")
            
            next_page_token = response.get('nextPageToken')
            
            interval = response.get('pollingIntervalMillis', 5000) / 1000
            # API 할당량 절약을 위해 최소 10초 간격으로 설정
            # 하루 종일 사용 가능 (시간당 360회, 일일 8,640회)
            time.sleep(max(interval, 10))
            
    except Exception as e:
        print(f"❌ 폴링 오류: {e}")
        traceback.print_exc()
    finally:
        is_tracking = False
        message_queue.put(None)
        print("⏹️ 채팅 수집 종료")

# Flask 라우트들
@app.route('/')
def index():
    """메인 웹 인터페이스"""
    return render_template_string(WEB_TEMPLATE)

@app.route('/popup')
def popup():
    """투명 팝업 순위창"""
    # URL 파라미터로 설정 받기
    size = request.args.get('size', 'normal')
    theme = request.args.get('theme', 'white')
    opacity = request.args.get('opacity', 'medium')
    
    # 템플릿에 설정 전달
    template = POPUP_TEMPLATE.replace('{{initial_size}}', size)
    template = template.replace('{{initial_theme}}', theme)
    template = template.replace('{{initial_opacity}}', opacity)
    
    return render_template_string(template)

@app.route('/authenticate')
def authenticate():
    """API 인증"""
    try:
        if chat_tracker.authenticate():
            return jsonify({'status': 'success', 'message': '✅ 인증 성공! 이제 추적을 시작할 수 있습니다.'})
        else:
            return jsonify({'status': 'error', 'message': '❌ 인증 실패. API 키를 확인하세요.'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'인증 오류: {str(e)}'})

@app.route('/start_tracking/<video_id>')
def start_tracking(video_id):
    """추적 시작"""
    global is_tracking, tracking_thread, db_thread, next_page_token
    
    if not chat_tracker.authenticated:
        return jsonify({'status': 'error', 'message': '먼저 인증을 완료하세요 🔑'})
    
    if is_tracking:
        return jsonify({'status': 'error', 'message': '이미 추적이 진행 중입니다 ⚠️'})
    
    # 페이지 토큰 초기화 (이전 세션의 토큰이 남아있을 수 있음)
    next_page_token = None
    
    # 메시지 큐 비우기
    while not message_queue.empty():
        try:
            message_queue.get_nowait()
        except:
            break
    
    is_tracking = True
    
    # DB 워커 스레드 확인 및 시작
    if not db_thread or not db_thread.is_alive():
        db_thread = threading.Thread(target=db_worker)
        db_thread.daemon = True
        db_thread.start()
    
    # 채팅 수집 스레드 시작
    tracking_thread = threading.Thread(target=chat_polling_worker, args=(video_id,))
    tracking_thread.daemon = True
    tracking_thread.start()
    
    return jsonify({'status': 'success', 'message': f'🚀 {video_id} 추적 시작! 채팅 데이터를 수집하고 있습니다.'})

@app.route('/stop_tracking')
def stop_tracking():
    """추적 중지"""
    global is_tracking, tracking_thread
    is_tracking = False
    
    # 추적 스레드가 종료될 때까지 대기
    if tracking_thread and tracking_thread.is_alive():
        tracking_thread.join(timeout=5.0)
    
    return jsonify({'status': 'success', 'message': '⏹️ 추적이 중지되었습니다.'})

@app.route('/reset_database')
def reset_database():
    """데이터베이스 초기화"""
    global is_tracking, tracking_thread, db_thread, next_page_token
    
    # 추적 중인지 확인
    if is_tracking:
        return jsonify({'status': 'error', 'message': '추적 중에는 리셋할 수 없습니다. 먼저 추적을 중지하세요.'})
    
    # 추적 스레드가 완전히 종료되었는지 확인
    if tracking_thread and tracking_thread.is_alive():
        return jsonify({'status': 'error', 'message': '추적이 완전히 중지될 때까지 잠시 기다려주세요.'})
    
    try:
        # DB 워커 스레드 종료
        if db_thread and db_thread.is_alive():
            message_queue.put(None)  # 종료 신호
            db_thread.join(timeout=2.0)  # 최대 2초 대기
        
        # 메시지 큐 완전히 비우기
        while not message_queue.empty():
            try:
                message_queue.get_nowait()
            except:
                break
        
        # 다음 페이지 토큰 초기화
        next_page_token = None
        
        # DB 연결 및 데이터 삭제
        conn = sqlite3.connect('youtube_chat.db', timeout=10.0)
        conn.execute('PRAGMA journal_mode=WAL')
        cursor = conn.cursor()
        
        # 모든 테이블 데이터 삭제
        cursor.execute('DELETE FROM messages')
        cursor.execute('DELETE FROM user_stats')
        cursor.execute('DELETE FROM system_info')
        
        # 변경사항 커밋
        conn.commit()
        
        # VACUUM으로 DB 파일 크기 최적화
        cursor.execute('VACUUM')
        
        conn.close()
        
        # DB 워커 스레드 재시작
        db_thread = threading.Thread(target=db_worker)
        db_thread.daemon = True
        db_thread.start()
        
        print("✅ 데이터베이스 완전 리셋 완료")
        
        return jsonify({
            'status': 'success', 
            'message': '✅ 모든 데이터가 완전히 초기화되었습니다!'
        })
    except Exception as e:
        print(f"❌ 리셋 오류: {e}")
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'리셋 실패: {str(e)}'})
    finally:
        # 안전을 위해 DB 연결 확실히 종료
        try:
            conn.close()
        except:
            pass

@app.route('/get_rankings')
def get_rankings():
    """순위 조회"""
    try:
        conn = sqlite3.connect('youtube_chat.db', timeout=10.0)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT username, message_count, last_message_time
            FROM user_stats
            WHERE message_count > 0
            ORDER BY message_count DESC, last_message_time DESC
            LIMIT 50
        ''')
        
        results = cursor.fetchall()
        conn.close()
        
        rankings = []
        for i, (username, message_count, last_message_time) in enumerate(results):
            rankings.append({
                'rank': i + 1,
                'username': username,
                'messageCount': message_count,
                'lastMessage': last_message_time
            })
        
        return jsonify(rankings)
    except Exception as e:
        print(f"순위 조회 오류: {e}")
        return jsonify([])

@app.route('/get_rankings_top5')
def get_rankings_top5():
    """상위 5명만 조회 (팝업용)"""
    try:
        conn = sqlite3.connect('youtube_chat.db', timeout=10.0)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT username, message_count
            FROM user_stats
            WHERE message_count > 0
            ORDER BY message_count DESC
            LIMIT 5
        ''')
        
        results = cursor.fetchall()
        conn.close()
        
        rankings = []
        for i, (username, message_count) in enumerate(results):
            rankings.append({
                'rank': i + 1,
                'username': username
            })
        
        return jsonify(rankings)
    except Exception as e:
        print(f"순위 조회 오류: {e}")
        return jsonify([])

@app.route('/get_stats')
def get_stats():
    """통계 조회"""
    try:
        conn = sqlite3.connect('youtube_chat.db', timeout=10.0)
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM messages')
        total_messages = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM messages WHERE message_type = 'filtered'")
        filtered_messages = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM user_stats WHERE message_count > 0')
        active_users = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM messages WHERE message_type = 'counted'")
        counted_messages = cursor.fetchone()[0]
        
        conn.close()
        
        return jsonify({
            'totalMessages': total_messages,
            'filteredMessages': filtered_messages,
            'activeUsers': active_users,
            'countedMessages': counted_messages
        })
    except Exception as e:
        print(f"통계 조회 오류: {e}")
        return jsonify({
            'totalMessages': 0,
            'filteredMessages': 0,
            'activeUsers': 0,
            'countedMessages': 0
        })

# 팝업 템플릿
POPUP_TEMPLATE = '''
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>채팅 순위 TOP 5</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            background: transparent;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            color: white;
            overflow: hidden;
        }
        
        .popup-container {
            background: rgba(0, 0, 0, 0.7);
            border-radius: 15px;
            padding: 20px;
            margin: 20px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        
        .popup-header {
            text-align: center;
            margin-bottom: 15px;
            margin-top: 5px;
            padding-bottom: 10px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.2);
        }
        
        .popup-header h2 {
            font-size: 2em;
            font-weight: bold;
            color: #FFE066;
            text-shadow: 2px 2px 4px rgba(0, 0, 0, 0.5);
            margin: 0;
        }
        
        .ranking-item {
            display: flex;
            align-items: center;
            padding: 10px;
            margin: 5px 0;
            background: rgba(255, 255, 255, 0.1);
            border-radius: 10px;
            transition: all 0.3s ease;
        }
        
        .ranking-item:hover {
            background: rgba(255, 255, 255, 0.2);
            transform: translateX(5px);
        }
        
        .rank {
            font-size: 1.8em;
            font-weight: bold;
            width: 50px;
            text-align: center;
        }
        
        .rank.gold { color: #FFD700; text-shadow: 0 0 10px #FFD700; }
        .rank.silver { color: #C0C0C0; text-shadow: 0 0 10px #C0C0C0; }
        .rank.bronze { color: #CD7F32; text-shadow: 0 0 10px #CD7F32; }
        .rank.other { color: #FFE066; text-shadow: 0 0 8px #FFE066; }
        
        .username {
            flex: 1;
            font-size: 1.5em;
            font-weight: 600;
            margin-left: 10px;
            text-overflow: ellipsis;
            overflow: hidden;
            white-space: nowrap;
            color: #FFE066;
            text-shadow: 1px 1px 2px rgba(0, 0, 0, 0.5);
        }
        
        .no-data {
            text-align: center;
            padding: 40px 20px;
            color: #FFE066;
        }
    </style>
</head>
<body>
    <div class="popup-container" id="popupContainer">
        <div class="popup-header">
            <h2>🏆 현재 응원순위</h2>
        </div>
        
        <div id="rankingsList">
            <div class="no-data">
                <p>순위 데이터를 불러오는 중...</p>
            </div>
        </div>
    </div>

    <script>
        async function refreshRankings() {
            try {
                const response = await fetch('/get_rankings_top5');
                const rankings = await response.json();
                updateRankings(rankings);
            } catch (error) {
                console.error('순위 새로고침 실패:', error);
            }
        }
        
        function updateRankings(rankings) {
            const rankingsList = document.getElementById('rankingsList');
            
            if (!rankings || rankings.length === 0) {
                rankingsList.innerHTML = `
                    <div class="no-data">
                        <p>아직 순위 데이터가 없습니다</p>
                    </div>
                `;
                return;
            }
            
            let html = '';
            rankings.forEach((user, index) => {
                let rankClass = '';
                let rankEmoji = '';
                
                if (index === 0) { rankClass = 'gold'; rankEmoji = '🥇'; }
                else if (index === 1) { rankClass = 'silver'; rankEmoji = '🥈'; }
                else if (index === 2) { rankClass = 'bronze'; rankEmoji = '🥉'; }
                else { rankClass = 'other'; rankEmoji = `${user.rank}`; }
                
                html += `
                    <div class="ranking-item">
                        <div class="rank ${rankClass}">${rankEmoji}</div>
                        <div class="username">${user.username}</div>
                    </div>
                `;
            });
            
            rankingsList.innerHTML = html;
        }
        
        // 초기 로드 및 자동 새로고침
        refreshRankings();
        setInterval(refreshRankings, 3000); // 3초마다 새로고침
    </script>
</body>
</html>
'''

# 웹 템플릿
WEB_TEMPLATE = '''
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🏆 YouTube 채팅 순위</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh; color: #333;
        }
        .container { max-width: 900px; margin: 0 auto; padding: 20px; }
        
        .header {
            background: rgba(255,255,255,0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px; padding: 30px; text-align: center;
            margin-bottom: 20px; box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        }
        .header h1 {
            font-size: 2.5em; margin-bottom: 10px;
            background: linear-gradient(45deg, #ff6b6b, #4ecdc4);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }
        
        .controls {
            background: rgba(255,255,255,0.9); border-radius: 15px;
            padding: 25px; margin-bottom: 20px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.1);
        }
        .input-group { display: flex; gap: 10px; margin-bottom: 15px; flex-wrap: wrap; }
        input[type="text"] {
            flex: 1; padding: 12px 16px; border: 2px solid #e1e5e9;
            border-radius: 10px; font-size: 16px; min-width: 300px;
        }
        button {
            padding: 12px 24px; border: none; border-radius: 10px;
            font-size: 16px; font-weight: 600; cursor: pointer;
            transition: all 0.3s ease; white-space: nowrap;
        }
        .btn-primary { background: #4CAF50; color: white; }
        .btn-primary:hover { background: #45a049; transform: translateY(-2px); }
        .btn-danger { background: #f44336; color: white; }
        .btn-danger:hover { background: #da190b; transform: translateY(-2px); }
        .btn-info { background: #2196F3; color: white; }
        .btn-info:hover { background: #0b7dda; transform: translateY(-2px); }
        .btn-warning { background: #ff9800; color: white; }
        .btn-warning:hover { background: #e68900; transform: translateY(-2px); }
        
        .status {
            background: rgba(255,255,255,0.9); border-radius: 15px;
            padding: 20px; margin-bottom: 20px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.1);
        }
        .status-grid {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 15px;
        }
        .status-card {
            background: linear-gradient(45deg, #667eea, #764ba2);
            color: white; padding: 20px; border-radius: 12px; text-align: center;
        }
        .status-card h3 { font-size: 1.8em; margin-bottom: 5px; }
        
        .rankings {
            background: rgba(255,255,255,0.95); border-radius: 15px;
            overflow: hidden; box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        }
        .rankings-header {
            background: linear-gradient(45deg, #ff6b6b, #4ecdc4);
            color: white; padding: 20px; text-align: center;
        }
        .ranking-item {
            display: grid; grid-template-columns: 80px 1fr 120px;
            gap: 15px; padding: 15px 20px; border-bottom: 1px solid #f0f0f0;
            align-items: center; transition: background 0.3s ease;
        }
        .ranking-item:hover { background: #f8f9fa; }
        .rank {
            font-size: 1.5em; font-weight: bold; text-align: center;
        }
        .rank.gold { color: #FFD700; }
        .rank.silver { color: #C0C0C0; }
        .rank.bronze { color: #CD7F32; }
        .username { font-weight: 600; color: #333; font-size: 1.1em; }
        .message-count {
            background: #e3f2fd; color: #1976d2;
            padding: 8px 12px; border-radius: 20px;
            font-weight: 600; text-align: center;
        }
        
        .message { 
            margin: 10px 0; padding: 12px 16px; border-radius: 8px; 
            font-weight: 500; border-left: 4px solid;
        }
        .success { 
            background: #d4edda; color: #155724; 
            border-left-color: #28a745;
        }
        .error { 
            background: #f8d7da; color: #721c24; 
            border-left-color: #dc3545;
        }
        .info { 
            background: #d1ecf1; color: #0c5460; 
            border-left-color: #17a2b8;
        }
        
        .loading {
            text-align: center; padding: 40px; color: #666;
            font-size: 1.1em;
        }
        
        .help-text {
            font-size: 0.9em; color: #666; margin-top: 8px;
            font-style: italic;
        }
        
        @media (max-width: 768px) {
            .input-group { flex-direction: column; }
            input[type="text"] { min-width: auto; }
            .ranking-item { 
                grid-template-columns: 60px 1fr;
                gap: 10px;
            }
            .message-count { 
                grid-column: 2; margin-top: 5px;
                justify-self: start;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🏆 YouTube 채팅 순위</h1>
            <p>실시간 유튜브 라이브 채팅 참여도 순위를 확인하세요!</p>
        </div>

        <div class="controls">
            <div class="input-group">
                <input type="text" id="videoId" placeholder="YouTube 비디오 ID 또는 URL 입력" />
                <button class="btn-primary" onclick="startTracking()">▶️ 추적 시작</button>
            </div>
            <div class="help-text">
                💡 YouTube URL 예시: https://www.youtube.com/watch?v=dQw4w9WgXcQ<br>
                또는 비디오 ID만: dQw4w9WgXcQ
            </div>
            <div class="input-group" style="margin-top: 15px;">
                <button class="btn-primary" onclick="authenticate()">🔑 인증</button>
                <button class="btn-danger" onclick="stopTracking()">⏹️ 추적 중지</button>
                <button class="btn-info" onclick="refreshRankings()">🔄 새로고침</button>
                <button class="btn-info" onclick="openPopup()">📊 순위 팝업</button>
                <button class="btn-warning" onclick="resetDatabase()" style="margin-left: auto;">🗑️ 데이터 리셋</button>
            </div>
            <div id="messages"></div>
        </div>

        <div class="status">
            <div class="status-grid" id="statusGrid">
                <div class="status-card">
                    <h3 id="totalMessages">0</h3>
                    <p>총 메시지</p>
                </div>
                <div class="status-card">
                    <h3 id="activeUsers">0</h3>
                    <p>활성 사용자</p>
                </div>
                <div class="status-card">
                    <h3 id="countedMessages">0</h3>
                    <p>순위 집계</p>
                </div>
                <div class="status-card">
                    <h3 id="filteredMessages">0</h3>
                    <p>필터링됨</p>
                </div>
            </div>
        </div>

        <div class="rankings">
            <div class="rankings-header">
                <h2>📊 실시간 채팅 참여도 순위</h2>
                <p>메시지 전송 횟수 기준 • 자동 새로고침</p>
            </div>
            <div id="rankingsList">
                <div class="loading">
                    <p>🔄 순위 데이터를 불러오는 중...</p>
                    <p style="font-size: 0.9em; margin-top: 10px;">먼저 인증을 완료하고 추적을 시작하세요!</p>
                </div>
            </div>
        </div>
    </div>

    <script>
        let refreshInterval;
        let isTracking = false;

        function showMessage(text, type = 'info') {
            const messages = document.getElementById('messages');
            const div = document.createElement('div');
            div.className = `message ${type}`;
            div.textContent = text;
            messages.appendChild(div);
            setTimeout(() => div.remove(), 5000);
            
            div.scrollIntoView({ behavior: 'smooth' });
        }

        function extractVideoId(input) {
            if (!input) return '';
            
            const patterns = [
                /[?&]v=([^&]+)/,
                r'youtu\.be/([^?]+)'
                /[?&]vi=([^&]+)/,
            ];
            
            for (const pattern of patterns) {
                const match = input.match(pattern);
                if (match) return match[1];
            }
            
            if (/^[a-zA-Z0-9_-]{11}$/.test(input)) {
                return input;
            }
            
            return input;
        }

        async function authenticate() {
            try {
                showMessage('🔑 API 키 인증을 진행합니다...', 'info');
                const response = await fetch('/authenticate');
                const data = await response.json();
                showMessage(data.message, data.status === 'success' ? 'success' : 'error');
            } catch (error) {
                showMessage('인증 요청 실패: ' + error.message, 'error');
            }
        }

        async function startTracking() {
            const input = document.getElementById('videoId').value.trim();
            if (!input) {
                showMessage('⚠️ YouTube 비디오 ID 또는 URL을 입력하세요', 'error');
                return;
            }

            const videoId = extractVideoId(input);
            if (!videoId) {
                showMessage('⚠️ 올바른 YouTube URL 또는 비디오 ID를 입력하세요', 'error');
                return;
            }

            try {
                showMessage(`🚀 ${videoId} 추적을 시작합니다...`, 'info');
                const response = await fetch(`/start_tracking/${videoId}`);
                const data = await response.json();
                showMessage(data.message, data.status === 'success' ? 'success' : 'error');
                
                if (data.status === 'success') {
                    isTracking = true;
                    startAutoRefresh();
                    showMessage('✨ 3초마다 자동으로 순위가 업데이트됩니다', 'info');
                }
            } catch (error) {
                showMessage('추적 시작 실패: ' + error.message, 'error');
            }
        }

        async function stopTracking() {
            try {
                const response = await fetch('/stop_tracking');
                const data = await response.json();
                showMessage(data.message, 'success');
                isTracking = false;
                stopAutoRefresh();
            } catch (error) {
                showMessage('추적 중지 실패: ' + error.message, 'error');
            }
        }

        async function resetDatabase() {
            if (!confirm('⚠️ 정말로 모든 데이터를 삭제하시겠습니까?')) {
                return;
            }
            
            try {
                showMessage('🔄 데이터 리셋 중...', 'info');
                const response = await fetch('/reset_database');
                const data = await response.json();
                
                showMessage(data.message, data.status === 'success' ? 'success' : 'error');
                if (data.status === 'success') {
                    refreshRankings();
                }
            } catch (error) {
                showMessage('리셋 실패: ' + error.message, 'error');
            }
        }

        function openPopup() {
            const popupWindow = window.open(
                '/popup', 
                'rankingsPopup', 
                'width=350,height=400,resizable=yes,scrollbars=no,toolbar=no,menubar=no,location=no,directories=no,status=no'
            );
            
            if (popupWindow) {
                popupWindow.focus();
                showMessage('📊 순위 팝업창이 열렸습니다!', 'success');
            } else {
                showMessage('⚠️ 팝업 차단됨! 브라우저 설정에서 팝업을 허용해주세요.', 'error');
            }
        }

        async function refreshRankings() {
            try {
                const rankingsResponse = await fetch('/get_rankings');
                const rankings = await rankingsResponse.json();
                
                const statsResponse = await fetch('/get_stats');
                const stats = await statsResponse.json();
                
                updateRankings(rankings);
                updateStats(stats);
            } catch (error) {
                console.error('데이터 새로고침 실패:', error);
            }
        }

        function updateRankings(rankings) {
            const rankingsList = document.getElementById('rankingsList');
            
            if (!rankings || rankings.length === 0) {
                rankingsList.innerHTML = `
                    <div class="loading">
                        <p>📭 아직 순위 데이터가 없습니다</p>
                        <p style="font-size: 0.9em; margin-top: 10px;">
                            라이브 방송에서 채팅이 활발해지면 순위가 나타납니다!
                        </p>
                    </div>
                `;
                return;
            }

            let html = '';
            rankings.forEach((user, index) => {
                let rankClass = '';
                let rankEmoji = '';
                
                if (index === 0) { rankClass = 'gold'; rankEmoji = '🥇'; }
                else if (index === 1) { rankClass = 'silver'; rankEmoji = '🥈'; }
                else if (index === 2) { rankClass = 'bronze'; rankEmoji = '🥉'; }
                else { rankEmoji = `${user.rank}위`; }
                
                html += `
                    <div class="ranking-item">
                        <div class="rank ${rankClass}">${rankEmoji}</div>
                        <div class="username">${user.username}</div>
                        <div class="message-count">${user.messageCount}개</div>
                    </div>
                `;
            });
            
            rankingsList.innerHTML = html;
        }

        function updateStats(stats) {
            document.getElementById('totalMessages').textContent = (stats.totalMessages || 0).toLocaleString();
            document.getElementById('activeUsers').textContent = (stats.activeUsers || 0).toLocaleString();
            document.getElementById('countedMessages').textContent = (stats.countedMessages || 0).toLocaleString();
            document.getElementById('filteredMessages').textContent = (stats.filteredMessages || 0).toLocaleString();
        }

        function startAutoRefresh() {
            stopAutoRefresh();
            refreshRankings();
            refreshInterval = setInterval(refreshRankings, 3000);
        }

        function stopAutoRefresh() {
            if (refreshInterval) {
                clearInterval(refreshInterval);
                refreshInterval = null;
            }
        }

        document.addEventListener('DOMContentLoaded', function() {
            refreshRankings();
            
            document.getElementById('videoId').addEventListener('keypress', function(e) {
                if (e.key === 'Enter') {
                    startTracking();
                }
            });
        });
    </script>
</body>
</html>
'''

if __name__ == '__main__':
    print("=" * 60)
    print("🚀 YouTube 채팅 순위 프로그램 v3.3 (수정본)")
    print("=" * 60)
    print()
    
    init_database()
    
    db_thread = threading.Thread(target=db_worker)
    db_thread.daemon = True
    db_thread.start()
    
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
    except:
        local_ip = '127.0.0.1'
    
    print("⚙️  설정 확인:")
    print(f"   API 키: ✅ 설정됨 ({API_KEY[:10]}...)")
    print(f"   데이터베이스: ✅ youtube_chat.db")
    print()
    print("🌐 접속 주소:")
    print(f"   📱 본인 PC: http://localhost:5000")
    print(f"   💻 다른 PC: http://{local_ip}:5000")
    print()
    print("📋 사용 방법:")
    print("1. 웹 브라우저에서 접속")
    print("2. 🔑 인증 버튼 클릭")
    print("3. YouTube 라이브 URL 입력")
    print("4. ▶️ 추적 시작")
    print("5. 📊 순위 팝업 버튼으로 투명 순위창 열기")
    print()
    print("💡 팝업창 기능:")
    print("   - ⚙️ 설정 패널")
    print("   - 글씨 크기 3단계 조절")
    print("   - 5가지 색상 테마")
    print("   - 🎨 투명도 3단계 조절")
    print()
    print("⏹️  종료: Ctrl+C")
    print("=" * 60)
    
 try:
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port, threaded=True)
except KeyboardInterrupt:
        print("\n\n👋 프로그램을 종료합니다.")
        is_tracking = False
        message_queue.put(None)

