# streamlit_app.py - Complete Facebook Message Bot with Streamlit UI (NO AUTO-RERUN)
# All original functionality preserved - Browser restart every 3 hours, hard kill, auto-resume

import streamlit as st
import os
import sys
import time
import json
import random
import sqlite3
import threading
import gc
import subprocess
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass
from collections import deque
from contextlib import contextmanager

from cryptography.fernet import Fernet
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# ==================== CONFIGURATION ====================
MAX_TASKS = 50
BROWSER_RESTART_HOURS = 3

# Data directory for persistence
DATA_DIR = Path('/app/data') if os.path.exists('/app') else Path(__file__).parent / 'data'
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / 'bot_data.db'
ENCRYPTION_KEY_FILE = DATA_DIR / '.encryption_key'

# ==================== HARD KILL FUNCTION ====================
def hard_kill_all_chromium(task_id: str = ""):
    """Force kill ALL chromium processes - minimal wait"""
    try:
        subprocess.run(['pkill', '-9', '-f', 'chromium'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        subprocess.run(['pkill', '-9', '-f', 'chromedriver'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        subprocess.run(['pkill', '-9', '-f', 'chrome'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        subprocess.run(['rm', '-rf', '/dev/shm/.org.chromium*'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        time.sleep(1)
        if task_id:
            log_message(task_id, "🔪 Hard kill completed")
    except:
        pass

# ==================== LOGGING ====================
def log_message(task_id: str, msg: str):
    timestamp = time.strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {msg}"
    
    if 'task_logs' not in st.session_state:
        st.session_state.task_logs = {}
    
    if task_id not in st.session_state.task_logs:
        st.session_state.task_logs[task_id] = deque(maxlen=100)
    
    st.session_state.task_logs[task_id].append(formatted_msg)
    print(formatted_msg)

# ==================== ENCRYPTION ====================
def get_encryption_key():
    if ENCRYPTION_KEY_FILE.exists():
        with open(ENCRYPTION_KEY_FILE, 'rb') as f:
            return f.read()
    else:
        key = Fernet.generate_key()
        with open(ENCRYPTION_KEY_FILE, 'wb') as f:
            f.write(key)
        return key

ENCRYPTION_KEY = get_encryption_key()
cipher_suite = Fernet(ENCRYPTION_KEY)

def encrypt_data(data):
    if not data:
        return None
    return cipher_suite.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data):
    if not encrypted_data:
        return ""
    try:
        return cipher_suite.decrypt(encrypted_data.encode()).decode()
    except:
        return ""

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            cookies_encrypted TEXT,
            chat_id TEXT,
            name_prefix TEXT,
            messages TEXT,
            delay INTEGER DEFAULT 30,
            status TEXT DEFAULT 'stopped',
            messages_sent INTEGER DEFAULT 0,
            rotation_index INTEGER DEFAULT 0,
            last_browser_restart TIMESTAMP,
            start_time TIMESTAMP,
            last_active TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('SELECT * FROM users WHERE username = "admin"')
    if not cursor.fetchone():
        password_hash = hashlib.sha256("admin123".encode()).hexdigest()
        cursor.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', 
                      ('admin', password_hash))
    
    conn.commit()
    conn.close()

init_db()

# ==================== TASK CLASS ====================
@dataclass
class Task:
    task_id: str
    username: str
    cookies: List[str]
    chat_id: str
    name_prefix: str
    messages: List[str]
    delay: int
    status: str
    messages_sent: int
    start_time: Optional[datetime]
    last_active: Optional[datetime]
    last_browser_restart: Optional[datetime]
    running: bool = False
    stop_flag: bool = False
    rotation_index: int = 0
    
    def get_uptime(self):
        if not self.start_time:
            return "00:00:00"
        delta = datetime.now() - self.start_time
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        seconds = delta.seconds % 60
        if days > 0:
            return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

# ==================== TASK MANAGER ====================
class TaskManager:
    def __init__(self):
        self.tasks: Dict[str, Task] = {}
        self.task_threads: Dict[str, threading.Thread] = {}
        self.load_tasks_from_db()
        self.start_auto_resume()
    
    def load_tasks_from_db(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM tasks')
        for row in cursor.fetchall():
            try:
                cookies = json.loads(decrypt_data(row[2])) if row[2] else []
                messages = json.loads(decrypt_data(row[5])) if row[5] else []
                
                task = Task(
                    task_id=row[0],
                    username=row[1],
                    cookies=cookies,
                    chat_id=row[3] or "",
                    name_prefix=row[4] or "",
                    messages=messages,
                    delay=row[6] or 30,
                    status=row[7] or "stopped",
                    messages_sent=row[8] or 0,
                    start_time=datetime.fromisoformat(row[11]) if row[11] else None,
                    last_active=datetime.fromisoformat(row[12]) if row[12] else None,
                    last_browser_restart=datetime.fromisoformat(row[10]) if row[10] else None,
                    rotation_index=row[9] or 0
                )
                self.tasks[task.task_id] = task
                if task.status == "running":
                    self.start_task(task.task_id)
            except Exception as e:
                print(f"Error loading task: {e}")
        conn.close()
    
    def save_task(self, task: Task):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO tasks 
            (task_id, username, cookies_encrypted, chat_id, name_prefix, messages, 
             delay, status, messages_sent, rotation_index, last_browser_restart, start_time, last_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task.task_id,
            task.username,
            encrypt_data(json.dumps(task.cookies)),
            task.chat_id,
            task.name_prefix,
            encrypt_data(json.dumps(task.messages)),
            task.delay,
            task.status,
            task.messages_sent,
            task.rotation_index,
            task.last_browser_restart.isoformat() if task.last_browser_restart else None,
            task.start_time.isoformat() if task.start_time else None,
            task.last_active.isoformat() if task.last_active else None
        ))
        conn.commit()
        conn.close()
    
    def delete_task(self, task_id: str):
        if task_id in self.tasks:
            self.stop_task(task_id)
            del self.tasks[task_id]
            if 'task_logs' in st.session_state and task_id in st.session_state.task_logs:
                del st.session_state.task_logs[task_id]
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))
            conn.commit()
            conn.close()
            return True
        return False
    
    def start_task(self, task_id: str):
        if task_id not in self.tasks:
            return False
        task = self.tasks[task_id]
        if task.status == "running":
            return False
        
        log_message(task_id, "🔥 Initial hard kill - cleaning memory...")
        hard_kill_all_chromium(task_id)
        time.sleep(2)
        
        if len([t for t in self.tasks.values() if t.status == "running"]) >= MAX_TASKS:
            return False
        task.status = "running"
        task.stop_flag = False
        if not task.start_time:
            task.start_time = datetime.now()
        if not task.last_browser_restart:
            task.last_browser_restart = datetime.now()
        task.last_active = datetime.now()
        self.save_task(task)
        
        thread = threading.Thread(target=self._run_task, args=(task_id,), daemon=True)
        thread.start()
        self.task_threads[task_id] = thread
        return True
    
    def stop_task(self, task_id: str):
        if task_id not in self.tasks:
            return False
        task = self.tasks[task_id]
        task.stop_flag = True
        task.status = "stopped"
        task.last_active = datetime.now()
        self.save_task(task)
        return True
    
    def start_auto_resume(self):
        def auto_resume():
            while True:
                try:
                    for task_id, task in self.tasks.items():
                        if task.status == "running" and not task.running:
                            log_message(task_id, f"🔄 Auto-resume: Task dead, restarting...")
                            hard_kill_all_chromium(task_id)
                            self.start_task(task_id)
                except Exception as e:
                    print(f"Auto resume error: {e}")
                time.sleep(60)
        
        thread = threading.Thread(target=auto_resume, daemon=True)
        thread.start()
        print("✅ Auto-resume thread started")
    
    def _setup_browser(self, task_id: str):
        chrome_options = Options()
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-setuid-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--disable-plugins')
        chrome_options.add_argument('--window-size=1280,720')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36')
        
        chrome_options.add_argument('--memory-pressure-off')
        chrome_options.add_argument('--max_old_space_size=128')
        chrome_options.add_argument('--js-flags="--max-old-space-size=128"')
        
        chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        
        chromium_paths = [
            '/usr/bin/chromium',
            '/usr/bin/chromium-browser',
            '/usr/bin/google-chrome',
            '/usr/bin/chrome'
        ]
        
        for chromium_path in chromium_paths:
            if Path(chromium_path).exists():
                chrome_options.binary_location = chromium_path
                log_message(task_id, f'Found Chromium at: {chromium_path}')
                break
        
        chromedriver_paths = [
            '/usr/bin/chromedriver',
            '/usr/local/bin/chromedriver'
        ]
        
        driver_path = None
        for driver_candidate in chromedriver_paths:
            if Path(driver_candidate).exists():
                driver_path = driver_candidate
                log_message(task_id, f'Found ChromeDriver at: {driver_path}')
                break
        
        try:
            if driver_path:
                service = Service(executable_path=driver_path)
                driver = webdriver.Chrome(service=service, options=chrome_options)
                log_message(task_id, 'Chrome started with detected ChromeDriver!')
            else:
                driver = webdriver.Chrome(options=chrome_options)
                log_message(task_id, 'Chrome started with default driver!')
            
            driver.set_window_size(1280, 720)
            log_message(task_id, 'Chrome browser setup completed successfully!')
            return driver
            
        except Exception as error:
            log_message(task_id, f'Browser setup failed: {error}')
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                log_message(task_id, 'Trying webdriver-manager...')
                service = Service(ChromeDriverManager().install())
                driver = webdriver.Chrome(service=service, options=chrome_options)
                log_message(task_id, 'Chrome started with webdriver-manager!')
                return driver
            except Exception as e:
                log_message(task_id, f'All browser setups failed: {e}')
                raise error
    
    def _find_message_input(self, driver, task_id: str, process_id: str):
        log_message(task_id, f"{process_id}: Finding message input...")
        
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(2)
        except Exception:
            pass
        
        message_input_selectors = [
            'div[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"][data-lexical-editor="true"]',
            'div[aria-label*="message" i][contenteditable="true"]',
            'div[aria-label*="Message" i][contenteditable="true"]',
            'div[contenteditable="true"][spellcheck="true"]',
            '[role="textbox"][contenteditable="true"]',
            'textarea[placeholder*="message" i]',
            'div[aria-placeholder*="message" i]',
            'div[data-placeholder*="message" i]',
            '[contenteditable="true"]',
            'textarea',
            'input[type="text"]'
        ]
        
        for idx, selector in enumerate(message_input_selectors):
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    try:
                        is_editable = driver.execute_script("""
                            return arguments[0].contentEditable === 'true' || 
                                   arguments[0].tagName === 'TEXTAREA' || 
                                   arguments[0].tagName === 'INPUT';
                        """, element)
                        
                        if is_editable:
                            try:
                                element.click()
                                time.sleep(0.5)
                            except:
                                pass
                            
                            element_text = driver.execute_script("return arguments[0].placeholder || arguments[0].getAttribute('aria-label') || arguments[0].getAttribute('aria-placeholder') || '';", element).lower()
                            
                            keywords = ['message', 'write', 'type', 'send', 'chat', 'msg', 'reply', 'text', 'aa']
                            if any(keyword in element_text for keyword in keywords):
                                log_message(task_id, f"{process_id}: ✅ Found message input")
                                return element
                            elif idx < 10:
                                log_message(task_id, f"{process_id}: Using primary selector editable element")
                                return element
                            elif selector == '[contenteditable="true"]' or selector == 'textarea' or selector == 'input[type="text"]':
                                log_message(task_id, f"{process_id}: Using fallback editable element")
                                return element
                    except Exception:
                        continue
            except Exception:
                continue
        
        log_message(task_id, f"{process_id}: ❌ Message input not found!")
        return None
    
    def _login_and_navigate(self, driver, task: Task, task_id: str, process_id: str):
        log_message(task_id, f"{process_id}: Loading Facebook homepage...")
        driver.get('https://www.facebook.com/')
        time.sleep(8)
        
        current_cookie = task.cookies[0] if task.cookies else ""
        if current_cookie and current_cookie.strip():
            log_message(task_id, f"{process_id}: Adding cookies...")
            cookie_array = current_cookie.split(';')
            for cookie in cookie_array:
                cookie_trimmed = cookie.strip()
                if cookie_trimmed and '=' in cookie_trimmed:
                    name, value = cookie_trimmed.split('=', 1)
                    try:
                        driver.add_cookie({
                            'name': name.strip(),
                            'value': value.strip(),
                            'domain': '.facebook.com',
                            'path': '/'
                        })
                    except:
                        pass
            driver.refresh()
            time.sleep(5)
        
        if task.chat_id:
            log_message(task_id, f"{process_id}: Opening conversation {task.chat_id}...")
            driver.get(f'https://www.facebook.com/messages/t/{task.chat_id.strip()}')
        else:
            log_message(task_id, f"{process_id}: Opening messages...")
            driver.get('https://www.facebook.com/messages')
        
        time.sleep(12)
        
        message_input = self._find_message_input(driver, task_id, process_id)
        
        if not message_input:
            log_message(task_id, f"{process_id}: Input not found, waiting additional 5 seconds...")
            time.sleep(5)
            message_input = self._find_message_input(driver, task_id, process_id)
        
        return message_input
    
    def _send_single_message(self, driver, message_input, task: Task, task_id: str, process_id: str):
        messages_list = [msg.strip() for msg in task.messages if msg.strip()]
        if not messages_list:
            messages_list = ['Hello!']
        
        msg_idx = task.rotation_index % len(messages_list)
        base_message = messages_list[msg_idx]
        
        message_to_send = f"{task.name_prefix} {base_message}" if task.name_prefix else base_message
        
        try:
            driver.execute_script("""
                const element = arguments[0];
                const message = arguments[1];
                
                element.scrollIntoView({behavior: 'smooth', block: 'center'});
                element.focus();
                element.click();
                
                if (element.tagName === 'DIV') {
                    element.textContent = message;
                    element.innerHTML = message;
                } else {
                    element.value = message;
                }
                
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
                element.dispatchEvent(new InputEvent('input', { bubbles: true, data: message }));
            """, message_input, message_to_send)
            
            time.sleep(1)
            
            sent = driver.execute_script("""
                const sendButtons = document.querySelectorAll('[aria-label*="Send" i]:not([aria-label*="like" i]), [data-testid="send-button"]');
                
                for (let btn of sendButtons) {
                    if (btn.offsetParent !== null) {
                        btn.click();
                        return 'button_clicked';
                    }
                }
                return 'button_not_found';
            """)
            
            if sent == 'button_not_found':
                driver.execute_script("""
                    const element = arguments[0];
                    element.focus();
                    
                    const events = [
                        new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }),
                        new KeyboardEvent('keypress', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }),
                        new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true })
                    ];
                    
                    events.forEach(event => element.dispatchEvent(event));
                """, message_input)
                log_message(task_id, f"{process_id}: ✅ Sent via Enter")
            else:
                log_message(task_id, f"{process_id}: ✅ Sent via button")
            
            task.messages_sent += 1
            task.rotation_index += 1
            task.last_active = datetime.now()
            self.save_task(task)
            
            log_message(task_id, f"{process_id}: Message #{task.messages_sent} sent. Rotation index: {task.rotation_index}")
            return True
            
        except Exception as send_error:
            log_message(task_id, f"{process_id}: Send error: {str(send_error)[:100]}")
            return False
    
    def _run_task(self, task_id: str):
        task = self.tasks[task_id]
        task.running = True
        process_id = f"TASK-{task_id[-6:]}"
        
        driver = None
        message_input = None
        consecutive_failures = 0
        
        while task.status == "running" and not task.stop_flag:
            try:
                current_time = datetime.now()
                last_restart = task.last_browser_restart
                
                if last_restart:
                    hours_since_restart = (current_time - last_restart).total_seconds() / 3600
                else:
                    hours_since_restart = BROWSER_RESTART_HOURS + 1
                
                if hours_since_restart >= BROWSER_RESTART_HOURS or driver is None:
                    log_message(task_id, f"{process_id}: 🔄 Browser restart - running for {hours_since_restart:.1f} hours...")
                    log_message(task_id, f"{process_id}: 📍 Resuming from message #{task.messages_sent + 1} (rotation index: {task.rotation_index})")
                    
                    if driver:
                        try:
                            driver.quit()
                        except:
                            pass
                        time.sleep(3)
                    
                    log_message(task_id, f"{process_id}: 🔪 Hard kill before browser restart...")
                    hard_kill_all_chromium(task_id)
                    time.sleep(2)
                    
                    log_message(task_id, f"{process_id}: Creating fresh browser session...")
                    driver = self._setup_browser(task_id)
                    
                    message_input = self._login_and_navigate(driver, task, task_id, process_id)
                    
                    if not message_input:
                        log_message(task_id, f"{process_id}: ❌ Failed to find message input! Retrying in 10 seconds...")
                        driver = None
                        time.sleep(10)
                        continue
                    
                    task.last_browser_restart = datetime.now()
                    self.save_task(task)
                    
                    log_message(task_id, f"{process_id}: ✅ Browser ready! Continuing from message #{task.messages_sent + 1} (rotation index: {task.rotation_index})")
                    consecutive_failures = 0
                    time.sleep(3)
                
                try:
                    if message_input:
                        message_input.is_enabled()
                    else:
                        raise Exception("Message input lost")
                except:
                    log_message(task_id, f"{process_id}: Message input lost, reconnecting...")
                    message_input = self._login_and_navigate(driver, task, task_id, process_id)
                    if not message_input:
                        driver = None
                        time.sleep(5)
                        continue
                
                success = self._send_single_message(driver, message_input, task, task_id, process_id)
                
                if success:
                    consecutive_failures = 0
                    log_message(task_id, f"{process_id}: Waiting {task.delay}s for next message...")
                    time.sleep(task.delay)
                else:
                    consecutive_failures += 1
                    log_message(task_id, f"{process_id}: Send failed ({consecutive_failures}/3). Retrying...")
                    
                    if consecutive_failures >= 3:
                        log_message(task_id, f"{process_id}: Too many failures, restarting browser...")
                        driver = None
                        consecutive_failures = 0
                    time.sleep(10)
                
                if task.messages_sent % 50 == 0 and task.messages_sent > 0:
                    try:
                        driver.execute_script("localStorage.clear(); sessionStorage.clear();")
                        gc.collect()
                    except:
                        pass
                
            except Exception as e:
                log_message(task_id, f"{process_id}: Error: {str(e)[:100]}")
                driver = None
                time.sleep(10)
        
        if driver:
            try:
                driver.quit()
                log_message(task_id, f"{process_id}: Browser closed")
            except:
                pass
        
        task.running = False
        if task_id in self.task_threads:
            del self.task_threads[task_id]

# ==================== STREAMLIT UI ====================
# Initialize session state
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False
if 'username' not in st.session_state:
    st.session_state.username = None
if 'task_manager' not in st.session_state:
    st.session_state.task_manager = None
if 'selected_task' not in st.session_state:
    st.session_state.selected_task = None
if 'refresh_counter' not in st.session_state:
    st.session_state.refresh_counter = 0

# Lazy initialization of task manager (only once)
if st.session_state.task_manager is None:
    st.session_state.task_manager = TaskManager()

task_manager = st.session_state.task_manager

# Page config
st.set_page_config(
    page_title="Facebook Message Bot",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS
st.markdown("""
<style>
    .stButton button {
        background-color: #667eea;
        color: white;
        border-radius: 5px;
        border: none;
        padding: 0.5rem 1rem;
    }
    .stButton button:hover {
        background-color: #5a67d8;
    }
    .status-running {
        background-color: #d4edda;
        color: #155724;
        padding: 3px 10px;
        border-radius: 3px;
        font-weight: bold;
        display: inline-block;
    }
    .status-stopped {
        background-color: #f8d7da;
        color: #721c24;
        padding: 3px 10px;
        border-radius: 3px;
        font-weight: bold;
        display: inline-block;
    }
    .log-container {
        background-color: #1e1e1e;
        color: #d4d4d4;
        border-radius: 5px;
        padding: 15px;
        font-family: 'Courier New', monospace;
        font-size: 12px;
        height: 400px;
        overflow-y: auto;
    }
    .log-error {
        color: #f48771;
    }
    .stat-card {
        background: linear-gradient(135deg, #667eea, #764ba2);
        border-radius: 10px;
        padding: 20px;
        text-align: center;
        color: white;
    }
    .task-card {
        background: #f8f9fa;
        border-radius: 10px;
        padding: 15px;
        margin-bottom: 10px;
        border-left: 4px solid #667eea;
        box-shadow: 0 2px 5px rgba(0,0,0,0.1);
    }
    .task-card.running {
        border-left-color: #28a745;
    }
    .task-card.stopped {
        border-left-color: #dc3545;
    }
    .success-toast {
        background-color: #28a745;
        color: white;
        padding: 10px;
        border-radius: 5px;
        margin-bottom: 10px;
    }
    .error-toast {
        background-color: #dc3545;
        color: white;
        padding: 10px;
        border-radius: 5px;
        margin-bottom: 10px;
    }
</style>
""", unsafe_allow_html=True)

# ==================== LOGIN PAGE ====================
def login_page():
    st.markdown("""
        <div style="text-align: center; padding: 50px;">
            <h1>🤖 Facebook Message Bot</h1>
            <p>Automated messaging system with browser restart and resume capabilities</p>
            <p>🔄 Browser restart every 3 hours | 🔪 Hard kill | 🔄 Auto-resume</p>
        </div>
    """, unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("### Login")
        username = st.text_input("Username", key="login_username")
        password = st.text_input("Password", type="password", key="login_password")
        
        if st.button("Login", use_container_width=True):
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            cursor.execute('SELECT * FROM users WHERE username = ? AND password_hash = ?', 
                          (username, password_hash))
            user = cursor.fetchone()
            conn.close()
            
            if user:
                st.session_state.logged_in = True
                st.session_state.username = username
                st.rerun()
            else:
                st.error("Invalid credentials! Default: admin / admin123")
        
        st.info("Default login: **admin** / **admin123**")
        st.caption("💾 Data persists across sessions | 🔄 Browser auto-restart every 3 hours")

# ==================== MAIN DASHBOARD ====================
def dashboard():
    # Header
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        st.title("🤖 Facebook Message Bot")
        st.caption(f"Logged in as: {st.session_state.username}")
    with col3:
        if st.button("🚪 Logout", use_container_width=True):
            st.session_state.logged_in = False
            st.session_state.username = None
            st.session_state.selected_task = None
            st.rerun()
    
    # Manual Refresh Button
    col_refresh, col_empty = st.columns([1, 5])
    with col_refresh:
        if st.button("🔄 Refresh Data", use_container_width=True):
            st.session_state.refresh_counter += 1
            st.rerun()
    
    # Stats row
    tasks = list(task_manager.tasks.values())
    user_tasks = [t for t in tasks if t.username == st.session_state.username]
    
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.markdown(f"""
            <div class="stat-card">
                <h3>Total Tasks</h3>
                <h2>{len(user_tasks)}</h2>
            </div>
        """, unsafe_allow_html=True)
    with col2:
        st.markdown(f"""
            <div class="stat-card">
                <h3>Running Tasks</h3>
                <h2>{sum(1 for t in user_tasks if t.status == 'running')}</h2>
            </div>
        """, unsafe_allow_html=True)
    with col3:
        st.markdown(f"""
            <div class="stat-card">
                <h3>Stopped Tasks</h3>
                <h2>{sum(1 for t in user_tasks if t.status == 'stopped')}</h2>
            </div>
        """, unsafe_allow_html=True)
    with col4:
        st.markdown(f"""
            <div class="stat-card">
                <h3>Total Messages</h3>
                <h2>{sum(t.messages_sent for t in user_tasks)}</h2>
            </div>
        """, unsafe_allow_html=True)
    
    # Success/Error message placeholder
    if 'action_message' in st.session_state:
        if st.session_state.action_message_type == 'success':
            st.success(st.session_state.action_message)
        elif st.session_state.action_message_type == 'error':
            st.error(st.session_state.action_message)
        del st.session_state.action_message
        del st.session_state.action_message_type
    
    # Main content
    col_left, col_right = st.columns([1, 1])
    
    with col_left:
        st.markdown("### ➕ Create New Task")
        with st.form("create_task_form"):
            chat_id = st.text_input("Chat Thread ID", placeholder="e.g., 1362400298935018", 
                                   help="Facebook chat thread ID from URL")
            name_prefix = st.text_input("Name Prefix (optional)", placeholder="e.g., John",
                                       help="Added before each message")
            messages = st.text_area("Messages (one per line)", 
                                   placeholder="Hello!\nHow are you?\nNice to meet you!", 
                                   height=120,
                                   help="Each line becomes a message in rotation")
            delay = st.number_input("Delay (seconds)", min_value=10, value=30, step=5,
                                   help="Time between messages")
            cookies = st.text_area("Facebook Cookies", 
                                  placeholder="c_user=1234567890; xs=789012%3Aabc123; datr=abc123",
                                  height=80,
                                  help="Copy cookies from browser after login")
            
            submitted = st.form_submit_button("🚀 Create & Start Task", use_container_width=True)
            if submitted:
                if not chat_id or not messages or not cookies:
                    st.error("Please fill all required fields!")
                else:
                    try:
                        task_id = f"task_{random.randint(10000, 99999)}"
                        messages_list = [m.strip() for m in messages.split('\n') if m.strip()]
                        
                        if not messages_list:
                            st.error("Please enter at least one message!")
                        else:
                            task = Task(
                                task_id=task_id,
                                username=st.session_state.username,
                                cookies=[cookies],
                                chat_id=chat_id,
                                name_prefix=name_prefix,
                                messages=messages_list,
                                delay=delay,
                                status='stopped',
                                messages_sent=0,
                                start_time=None,
                                last_active=None,
                                last_browser_restart=None,
                                rotation_index=0
                            )
                            
                            task_manager.tasks[task_id] = task
                            task_manager.save_task(task)
                            task_manager.start_task(task_id)
                            
                            st.session_state.action_message = f"✅ Task {task_id} created and started successfully!"
                            st.session_state.action_message_type = 'success'
                            st.rerun()
                    except Exception as e:
                        st.error(f"Error: {str(e)}")
    
    with col_right:
        st.markdown("### 📋 Your Tasks")
        
        if not user_tasks:
            st.info("No tasks created yet. Create your first task on the left!")
        else:
            for task in user_tasks:
                with st.container():
                    status_class = "running" if task.status == "running" else "stopped"
                    st.markdown(f"""
                        <div class="task-card {status_class}">
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <strong>📌 {task.task_id}</strong>
                                <span class="status-{status_class}">{task.status.upper()}</span>
                            </div>
                            <div style="font-size: 12px; color: #666; margin-top: 10px;">
                                💬 Chat: {task.chat_id[:20]}{'...' if len(task.chat_id) > 20 else ''} | 
                                📨 Sent: {task.messages_sent} msgs | 
                                ⏱️ Uptime: {task.get_uptime()} | 
                                ⏲️ Delay: {task.delay}s
                            </div>
                        </div>
                    """, unsafe_allow_html=True)
                    
                    col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 1])
                    with col_btn1:
                        if task.status == 'running':
                            if st.button(f"⏸ Stop", key=f"stop_{task.task_id}", use_container_width=True):
                                task_manager.stop_task(task.task_id)
                                st.session_state.action_message = f"⏸ Task {task.task_id} stopped"
                                st.session_state.action_message_type = 'success'
                                st.rerun()
                        else:
                            if st.button(f"▶ Start", key=f"start_{task.task_id}", use_container_width=True):
                                task_manager.start_task(task.task_id)
                                st.session_state.action_message = f"▶ Task {task.task_id} started"
                                st.session_state.action_message_type = 'success'
                                st.rerun()
                    with col_btn2:
                        if st.button(f"📄 Logs", key=f"logs_{task.task_id}", use_container_width=True):
                            st.session_state.selected_task = task.task_id
                            st.session_state.action_message = f"📄 Showing logs for {task.task_id}"
                            st.session_state.action_message_type = 'success'
                            st.rerun()
                    with col_btn3:
                        if st.button(f"🗑 Delete", key=f"delete_{task.task_id}", use_container_width=True):
                            if task_manager.delete_task(task.task_id):
                                if st.session_state.selected_task == task.task_id:
                                    st.session_state.selected_task = None
                                st.session_state.action_message = f"🗑 Task {task.task_id} deleted"
                                st.session_state.action_message_type = 'success'
                                st.rerun()
                    
                    st.divider()
    
    # Logs section
    st.markdown("### 📄 Task Logs")
    
    if st.session_state.selected_task and st.session_state.selected_task in task_manager.tasks:
        selected_task_id = st.session_state.selected_task
        logs = list(st.session_state.get('task_logs', {}).get(selected_task_id, []))
        
        col_refresh_logs, col_clear_logs = st.columns([1, 1])
        with col_refresh_logs:
            if st.button("🔄 Refresh Logs", use_container_width=True):
                st.rerun()
        with col_clear_logs:
            if st.button("🗑 Clear Logs", use_container_width=True):
                if selected_task_id in st.session_state.task_logs:
                    st.session_state.task_logs[selected_task_id].clear()
                st.rerun()
        
        if logs:
            log_html = '<div class="log-container">'
            for log in logs[-100:]:  # Last 100 logs
                is_error = 'ERROR' in log or 'Fatal' in log or '❌' in log
                log_class = 'log-error' if is_error else ''
                log_html += f'<div class="{log_class}">{log}</div>'
            log_html += '</div>'
            st.markdown(log_html, unsafe_allow_html=True)
            st.caption(f"📊 Showing last {len(logs[-100:])} logs | Total logs: {len(logs)}")
        else:
            st.info("No logs available for this task yet. Wait for task activity...")
    else:
        st.info("👈 Select a task's 'Logs' button to view its activity logs")
    
    # Footer with info
    st.divider()
    st.caption(f"""
    🔧 **System Info:**
    - 🔄 Browser Restart: Every {BROWSER_RESTART_HOURS} hours
    - 🔪 Hard kill: On Task Start & Browser Restart
    - 🔄 Auto-resume: Task crash par auto restart
    - 💾 Messages resume from exact rotation index after restart
    - ⏱️ Original timing: 8s, 5s, 12s waits
    - 📊 Refresh manually using 'Refresh Data' button
    """)

# ==================== RUN APP ====================
if not st.session_state.logged_in:
    login_page()
else:
    dashboard()
