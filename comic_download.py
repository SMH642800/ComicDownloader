import sys
import os
import re
import time
import ssl
import urllib3
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, 
                            QWidget, QLabel, QLineEdit, QPushButton, QTextEdit, 
                            QProgressBar, QSpinBox, QFileDialog, QMessageBox, QFrame)
from PyQt6.QtCore import QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont
import requests
from bs4 import BeautifulSoup

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class DownloadThread(QThread):
    progress_signal = pyqtSignal(str)
    progress_update_signal = pyqtSignal(str, str)  # 新增：用於更新同一行的進度
    overall_progress_signal = pyqtSignal(int, int)  # 新增：總體進度 (當前, 總數)
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)
    
    def __init__(self, base_url, start_page, end_page, output_folder, max_workers):
        super().__init__()
        self.base_url = base_url
        self.start_page = start_page
        self.end_page = end_page
        self.output_folder = output_folder
        self.max_workers = max_workers
        self.is_cancelled = False
        self.completed_count = 0
        self.total_count = 0
        
        # 設置 requests session 以提升效能
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })
        
        # 忽略 SSL 錯誤
        self.session.verify = False
        
    def cancel(self):
        self.is_cancelled = True
        
    def get_page_url(self, page_num):
        """根據頁數生成頁面 URL"""
        if 'page-' in self.base_url:
            return re.sub(r'page-\d+', f'page-{page_num}', self.base_url)
        else:
            # 如果原 URL 沒有頁碼，添加頁碼參數
            separator = '&' if '?' in self.base_url else '?'
            return f"{self.base_url}{separator}page={page_num}"
    
    def get_manga_links_from_page(self, page_url):
        """從頁面獲取所有漫畫連結"""
        try:
            self.progress_signal.emit(f"🔍 正在分析頁面: {page_url}")
            response = self.session.get(page_url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            manga_links = []
            
            # 尋找漫畫連結 - 根據網站結構調整選擇器
            # 一般漫畫網站的連結可能在這些地方
            link_selectors = [
                'a[href*="/photos-index-aid-"]',  # wnacg 特定格式
                '.pic_box a',
                '.gallery a',
                '.thumb a',
                'a[href*="aid"]',
                '.list-item a',
            ]
            
            for selector in link_selectors:
                links = soup.select(selector)
                if links:
                    for link in links:
                        href = link.get('href')
                        if href:
                            full_url = urljoin(page_url, href)
                            if full_url not in manga_links:
                                manga_links.append(full_url)
                    break
            
            self.progress_signal.emit(f"✅ 在此頁找到 {len(manga_links)} 個漫畫")
            return manga_links
            
        except Exception as e:
            self.progress_signal.emit(f"❌ 頁面分析失敗 {page_url}: {str(e)}")
            return []
    
    def get_download_link(self, manga_url):
        """從漫畫頁面獲取下載連結"""
        try:
            response = self.session.get(manga_url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # 尋找下載按鈕或連結
            download_selectors = [
                'a[href*="download"]',
                'a:contains("下載")',
                'a:contains("本地下載")',
                '.download-btn',
                '#download',
                'a[href*="down"]',
            ]
            
            for selector in download_selectors:
                if ':contains(' in selector:
                    # 處理包含文字的選擇器
                    text = selector.split(':contains("')[1].split('")')[0]
                    links = soup.find_all('a', string=lambda s: s and text in s)
                else:
                    links = soup.select(selector)
                
                if links:
                    download_page_url = urljoin(manga_url, links[0].get('href'))
                    return self.get_final_download_link(download_page_url)
            
            return None
            
        except Exception as e:
            self.progress_signal.emit(f"❌ 無法獲取下載連結 {manga_url}: {str(e)}")
            return None
    
    def get_final_download_link(self, download_page_url):
        """從下載頁面獲取最終下載連結"""
        try:
            response = self.session.get(download_page_url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # 尋找本地下載連結
            final_selectors = [
                'a:contains("本地下載一")',
                'a:contains("本地下載二")',
                'a:contains("本地下載")',
                'a[href*=".zip"]',
                'a[href*="download"]',
                '.download-link',
            ]
            
            for selector in final_selectors:
                if ':contains(' in selector:
                    text = selector.split(':contains("')[1].split('")')[0]
                    links = soup.find_all('a', string=lambda s: s and text in s)
                else:
                    links = soup.select(selector)
                
                if links:
                    return urljoin(download_page_url, links[0].get('href'))
            
            return None
            
        except Exception as e:
            self.progress_signal.emit(f"❌ 無法獲取最終下載連結: {str(e)}")
            return None
    
    def get_manga_title(self, manga_url):
        """獲取漫畫標題"""
        try:
            response = self.session.get(manga_url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # 嘗試多種標題選擇器
            title_selectors = ['h1', 'h2', '.title', '#title', '.manga-title']
            
            for selector in title_selectors:
                title_elem = soup.select_one(selector)
                if title_elem:
                    title = title_elem.get_text().strip()
                    # 清理檔案名稱中的非法字符
                    title = re.sub(r'[<>:"/\\|?*]', '_', title)
                    return title[:100]  # 限制長度
            
            # 如果沒找到標題，使用 URL 的一部分
            return f"manga_{manga_url.split('-')[-1]}"
            
        except Exception as e:
            return f"unknown_manga_{int(time.time())}"
    
    def download_file(self, download_url, filepath, title, max_retries=3):
        """下載檔案 - 包含重試機制"""
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    # 重試前等待，時間遞增
                    wait_time = attempt * 5
                    self.progress_signal.emit(f"⏳ 等待 {wait_time} 秒後重試: {title} (第 {attempt + 1} 次嘗試)")
                    time.sleep(wait_time)
                
                # 生成唯一的識別符用於更新同一行
                download_id = f"download_{hash(title) % 10000}"
                
                # 開始下載 - 使用 progress_update_signal 來更新同一行
                self.progress_update_signal.emit(download_id, f"📥 開始下載: {title}")
                
                # 添加更多 headers 來模擬真實瀏覽器
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'DNT': '1',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Site': 'none',
                    'Cache-Control': 'max-age=0',
                }
                
                response = self.session.get(download_url, stream=True, timeout=120, headers=headers)
                response.raise_for_status()
                
                total_size = int(response.headers.get('content-length', 0))
                downloaded_size = 0
                
                with open(filepath, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if self.is_cancelled:
                            return False
                        
                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            
                            if total_size > 0:
                                progress = (downloaded_size / total_size) * 100
                                progress_text = f"📥 下載中: {title} ({progress:.1f}%)"
                                self.progress_update_signal.emit(download_id, progress_text)
                
                self.progress_update_signal.emit(download_id, f"✅ 下載完成: {title}")
                return True
                
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 503:
                    # 503 錯誤，需要重試
                    if attempt < max_retries - 1:
                        self.progress_signal.emit(f"⚠️ 伺服器暫時無法使用: {title} - 將在 {(attempt + 1) * 5} 秒後重試")
                        continue
                    else:
                        self.progress_signal.emit(f"❌ 下載失敗 (已重試 {max_retries} 次): {title} - 伺服器暫時無法使用")
                        return False
                else:
                    self.progress_signal.emit(f"❌ 下載失敗: {title} - HTTP {e.response.status_code}")
                    return False
            except Exception as e:
                if attempt < max_retries - 1:
                    self.progress_signal.emit(f"⚠️ 下載出錯: {title} - {str(e)} (將重試)")
                    continue
                else:
                    self.progress_signal.emit(f"❌ 下載失敗 (已重試 {max_retries} 次): {title} - {str(e)}")
                    return False
        
        return False
    
    def process_manga(self, manga_url):
        """處理單個漫畫的下載"""
        if self.is_cancelled:
            return
        
        try:
            # 獲取漫畫標題
            title = self.get_manga_title(manga_url)
            
            # 檢查檔案是否已存在
            filepath = os.path.join(self.output_folder, f"{title}.zip")
            if os.path.exists(filepath):
                self.progress_signal.emit(f"⏭️ 跳過已存在: {title}")
                self.completed_count += 1
                self.overall_progress_signal.emit(self.completed_count, self.total_count)
                return
            
            # 獲取下載連結前稍作延遲
            time.sleep(1)  # 避免請求過於頻繁
            download_url = self.get_download_link(manga_url)
            if not download_url:
                self.progress_signal.emit(f"❌ 無法找到下載連結: {title}")
                self.completed_count += 1
                self.overall_progress_signal.emit(self.completed_count, self.total_count)
                return
            
            # 下載檔案
            success = self.download_file(download_url, filepath, title)
            if not success and os.path.exists(filepath):
                os.remove(filepath)  # 刪除未完成的檔案
            
            self.completed_count += 1
            self.overall_progress_signal.emit(self.completed_count, self.total_count)
                
        except Exception as e:
            self.progress_signal.emit(f"❌ 處理失敗: {str(e)}")
            self.completed_count += 1
            self.overall_progress_signal.emit(self.completed_count, self.total_count)
    
    def run(self):
        try:
            # 確保輸出資料夾存在
            os.makedirs(self.output_folder, exist_ok=True)
            
            # 收集所有漫畫連結
            all_manga_links = []
            for page_num in range(self.start_page, self.end_page + 1):
                if self.is_cancelled:
                    return
                
                page_url = self.get_page_url(page_num)
                manga_links = self.get_manga_links_from_page(page_url)
                all_manga_links.extend(manga_links)
                
                # 增加頁面間的延遲
                time.sleep(2)  # 增加到 2 秒避免被偵測
            
            self.progress_signal.emit(f"🎯 總共找到 {len(all_manga_links)} 個漫畫")
            self.total_count = len(all_manga_links)
            self.completed_count = 0
            self.overall_progress_signal.emit(0, self.total_count)
            
            # 降低並發數以避免 503 錯誤
            effective_workers = min(self.max_workers, 3)  # 最多 3 個同時下載
            self.progress_signal.emit(f"🚀 使用 {effective_workers} 個執行緒進行下載 (避免伺服器過載)")
            
            # 使用多執行緒下載，但加入延遲機制
            with ThreadPoolExecutor(max_workers=effective_workers) as executor:
                futures = []
                
                # 分批提交任務，避免一次性提交太多
                batch_size = 5
                for i in range(0, len(all_manga_links), batch_size):
                    if self.is_cancelled:
                        break
                    
                    batch = all_manga_links[i:i + batch_size]
                    for url in batch:
                        if self.is_cancelled:
                            break
                        futures.append(executor.submit(self.process_manga, url))
                    
                    # 批次間延遲
                    if i + batch_size < len(all_manga_links):
                        time.sleep(3)  # 每批次間延遲 3 秒
                
                for future in as_completed(futures):
                    if self.is_cancelled:
                        executor.shutdown(wait=False)
                        return
                    
                    try:
                        future.result()
                    except Exception as e:
                        self.progress_signal.emit(f"❌ 執行緒錯誤: {str(e)}")
            
            self.progress_signal.emit("🎉 所有下載任務完成！")
            self.finished_signal.emit()
            
        except Exception as e:
            self.error_signal.emit(f"程式錯誤: {str(e)}")

class MangaDownloaderGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.download_thread = None
        self.progress_lines = {}  # 用於追蹤進度行
        self.init_ui()
        
    def init_ui(self):
        self.setWindowTitle("漫畫批量下載器 v2.0")
        self.setGeometry(100, 100, 800, 600)
        
        # 主部件
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        
        # 標題
        title_label = QLabel("🎨 漫畫批量下載器")
        title_label.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        layout.addWidget(title_label)
        
        # 分隔線
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(line)
        
        # 網址輸入
        url_layout = QHBoxLayout()
        url_layout.addWidget(QLabel("🔗 搜尋結果網址:"))
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("請輸入搜尋結果的網址...")
        url_layout.addWidget(self.url_input)
        layout.addLayout(url_layout)
        
        # 頁數設置
        page_layout = QHBoxLayout()
        page_layout.addWidget(QLabel("📄 起始頁:"))
        self.start_page_input = QSpinBox()
        self.start_page_input.setMinimum(1)
        self.start_page_input.setValue(1)
        page_layout.addWidget(self.start_page_input)
        
        page_layout.addWidget(QLabel("結束頁:"))
        self.end_page_input = QSpinBox()
        self.end_page_input.setMinimum(1)
        self.end_page_input.setValue(1)
        page_layout.addWidget(self.end_page_input)
        layout.addLayout(page_layout)
        
        # 輸出資料夾
        folder_layout = QHBoxLayout()
        folder_layout.addWidget(QLabel("📂 輸出資料夾:"))
        self.folder_input = QLineEdit()
        self.folder_input.setText(os.path.join(os.getcwd(), "downloads"))
        folder_layout.addWidget(self.folder_input)
        
        self.browse_button = QPushButton("瀏覽")
        self.browse_button.clicked.connect(self.browse_folder)
        folder_layout.addWidget(self.browse_button)
        layout.addLayout(folder_layout)
        
        # 下載設置
        settings_layout = QHBoxLayout()
        settings_layout.addWidget(QLabel("🚀 同時下載數:"))
        self.workers_input = QSpinBox()
        self.workers_input.setMinimum(1)
        self.workers_input.setMaximum(3)  # 降低最大值
        self.workers_input.setValue(2)  # 降低預設值
        settings_layout.addWidget(self.workers_input)
        layout.addLayout(settings_layout)
        
        # 控制按鈕
        button_layout = QHBoxLayout()
        self.start_button = QPushButton("🚀 開始下載")
        self.start_button.clicked.connect(self.start_download)
        button_layout.addWidget(self.start_button)
        
        self.cancel_button = QPushButton("❌ 取消下載")
        self.cancel_button.clicked.connect(self.cancel_download)
        self.cancel_button.setEnabled(False)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)
        
        # 總體進度條
        layout.addWidget(QLabel("📊 總體進度:"))
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet('''
            QProgressBar {
                border: 2px solid #000;
                border-radius: 5px;
                text-align:center;
                height: 20px;
                width: 200px;
            }
            QProgressBar::chunk {
                background: #09c;
                width:1px;
            }
        ''')
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        
        # 日誌輸出
        layout.addWidget(QLabel("📋 下載日誌:"))
        self.log_text = QTextEdit()
        self.log_text.setMaximumHeight(200)
        layout.addWidget(self.log_text)
    
    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "選擇輸出資料夾")
        if folder:
            self.folder_input.setText(folder)
    
    def start_download(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "警告", "請輸入搜尋結果網址！")
            return
        
        output_folder = self.folder_input.text().strip()
        if not output_folder:
            QMessageBox.warning(self, "警告", "請選擇輸出資料夾！")
            return
        
        start_page = self.start_page_input.value()
        end_page = self.end_page_input.value()
        max_workers = self.workers_input.value()
        
        if start_page > end_page:
            QMessageBox.warning(self, "警告", "起始頁不能大於結束頁！")
            return
        
        # 開始下載
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.log_text.clear()
        self.progress_lines.clear()  # 清空進度行追蹤

        self.progress_bar.setVisible(True)
        self.progress_bar.setFormat("%p%")  # 格式化為百分比
        self.progress_bar.setRange(0, 100)  # 設置為百分比進度條
        
        self.download_thread = DownloadThread(url, start_page, end_page, output_folder, max_workers)
        self.download_thread.progress_signal.connect(self.update_log)
        self.download_thread.progress_update_signal.connect(self.update_progress_line)
        self.download_thread.overall_progress_signal.connect(self.update_overall_progress)
        self.download_thread.finished_signal.connect(self.download_finished)
        self.download_thread.error_signal.connect(self.download_error)
        self.download_thread.start()
    
    def cancel_download(self):
        if self.download_thread:
            self.download_thread.cancel()
            self.update_log("🛑 正在取消下載...")
    
    def update_log(self, message):
        self.log_text.append(f"[{time.strftime('%H:%M:%S')}] {message}")
        self.log_text.ensureCursorVisible()
    
    def update_progress_line(self, download_id, progress_text):
        """更新同一行的下載進度"""
        cursor = self.log_text.textCursor()
        
        if download_id in self.progress_lines:
            # 如果這個下載ID已經存在，更新該行
            line_number = self.progress_lines[download_id]
            
            # 移動到指定行
            cursor.movePosition(cursor.MoveOperation.Start)
            for _ in range(line_number):
                cursor.movePosition(cursor.MoveOperation.Down)
            
            # 選擇整行並替換
            cursor.select(cursor.SelectionType.LineUnderCursor)
            cursor.removeSelectedText()
            cursor.insertText(f"[{time.strftime('%H:%M:%S')}] {progress_text}")
        else:
            # 新的下載項目，添加新行
            current_line = self.log_text.document().blockCount()
            self.progress_lines[download_id] = current_line
            self.log_text.append(f"[{time.strftime('%H:%M:%S')}] {progress_text}")
        
        self.log_text.ensureCursorVisible()
    
    def update_overall_progress(self, current, total):
        """更新總體進度條"""
        if total > 0:
            percentage = int((current / total) * 100)
            self.progress_bar.setValue(percentage)
            self.progress_bar.setFormat(f"{percentage}% ({current}/{total})")
        else:
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("0% (0/0)")
    
    def download_finished(self):
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setVisible(False)
        QMessageBox.information(self, "完成", "所有下載任務已完成！")
    
    def download_error(self, error_msg):
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setVisible(False)
        QMessageBox.critical(self, "錯誤", f"下載過程中發生錯誤：\n{error_msg}")

def main():
    app = QApplication(sys.argv)
    window = MangaDownloaderGUI()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()