import os
import sys
import json
import time
import shutil
import re
import subprocess
from typing import Dict, List, Optional, Tuple
import datetime
from pathlib import Path
import random
from itertools import zip_longest

import yt_dlp
import httpx
from unsplash.api import Api as UnsplashApi
from unsplash.auth import Auth as UnsplashAuth
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import whisper
import openai

# 加载环境变量
load_dotenv()

# 检查必要的环境变量
required_env_vars = {
    'OPENROUTER_API_KEY': '用于OpenRouter API',
    'OPENROUTER_API_URL': '用于OpenRouter API',
    'OPENROUTER_APP_NAME': '用于OpenRouter API',
    'OPENROUTER_HTTP_REFERER': '用于OpenRouter API',
    'UNSPLASH_ACCESS_KEY': '用于图片搜索',
    'UNSPLASH_SECRET_KEY': '用于Unsplash认证',
    'UNSPLASH_REDIRECT_URI': '用于Unsplash回调'
}

missing_env_vars = []
for var, desc in required_env_vars.items():
    if not os.getenv(var):
        missing_env_vars.append(f"  - {var} ({desc})")

if missing_env_vars:
    print("注意：以下环境变量未设置：")
    print("\n".join(missing_env_vars))
    print("\n将使用基本功能继续运行（无AI优化和图片）。")
    print("如需完整功能，请在 .env 文件中设置相应的 API 密钥。")
    print("继续处理...\n")

# 配置代理
http_proxy = os.getenv('HTTP_PROXY')
https_proxy = os.getenv('HTTPS_PROXY')
proxies = {
    'http': http_proxy,
    'https': https_proxy
} if http_proxy and https_proxy else None

# 禁用 SSL 验证（仅用于开发环境）
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

# OpenRouter configuration
openrouter_api_key = os.getenv('OPENROUTER_API_KEY')
openrouter_app_name = os.getenv('OPENROUTER_APP_NAME', 'video_note_generator')
openrouter_http_referer = os.getenv('OPENROUTER_HTTP_REFERER', 'https://github.com')
openrouter_available = False

# 配置 OpenAI API
client = openai.OpenAI(
    api_key=openrouter_api_key,
    base_url="https://openrouter.ai/api/v1",
    default_headers={
        "HTTP-Referer": openrouter_http_referer,
        "X-Title": openrouter_app_name,
    }
)

# 选择要使用的模型
AI_MODEL = "google/gemini-pro"  # 使用 Gemini Pro 模型

# Test OpenRouter connection
if openrouter_api_key:
    try:
        print(f"正在测试 OpenRouter API 连接...")
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "user", "content": "你好"}
            ]
        )
        
        if response.choices:
            print("✅ OpenRouter API 连接测试成功")
            openrouter_available = True
    except Exception as e:
        print(f"⚠️ OpenRouter API 连接测试失败: {str(e)}")
        print("将继续尝试使用API，但可能会遇到问题")

# 检查Unsplash配置
unsplash_access_key = os.getenv('UNSPLASH_ACCESS_KEY')
unsplash_client = None

if unsplash_access_key:
    try:
        auth = UnsplashAuth(
            client_id=unsplash_access_key,
            client_secret=None,
            redirect_uri=None
        )
        unsplash_client = UnsplashApi(auth)
        print("✅ Unsplash API 配置成功")
    except Exception as e:
        print(f"❌ Failed to initialize Unsplash client: {str(e)}")

# 检查ffmpeg
ffmpeg_path = None
try:
    subprocess.run(["/opt/homebrew/bin/ffmpeg", "-version"], 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.PIPE)
    print("✅ ffmpeg is available at /opt/homebrew/bin/ffmpeg")
    ffmpeg_path = "/opt/homebrew/bin/ffmpeg"
except Exception:
    try:
        subprocess.run(["ffmpeg", "-version"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE)
        print("✅ ffmpeg is available (from PATH)")
        ffmpeg_path = "ffmpeg"
    except Exception as e:
        print(f"⚠️ ffmpeg not found: {str(e)}")

class DownloadError(Exception):
    """自定义下载错误类"""
    def __init__(self, message: str, platform: str, error_type: str, details: str = None):
        self.message = message
        self.platform = platform
        self.error_type = error_type
        self.details = details
        super().__init__(self.message)

class VideoNoteGenerator:
    def __init__(self, output_dir: str = "generated_notes"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

        self.openrouter_available = openrouter_available
        self.unsplash_client = unsplash_client
        self.ffmpeg_path = ffmpeg_path
        
        # 初始化whisper模型
        print("正在加载Whisper模型...")
        self.whisper_model = None
        try:
            self.whisper_model = whisper.load_model("medium")
            print("✅ Whisper模型加载成功")
        except Exception as e:
            print(f"⚠️ Whisper模型加载失败: {str(e)}")
            print("将在需要时重试加载")
        
        # 日志目录
        self.log_dir = os.path.join(self.output_dir, 'logs')
        os.makedirs(self.log_dir, exist_ok=True)
        
        # cookie目录
        self.cookie_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies')
        os.makedirs(self.cookie_dir, exist_ok=True)
        
        # 平台cookie文件
        self.platform_cookies = {
            'douyin': os.path.join(self.cookie_dir, 'douyin_cookies.txt'),
            'bilibili': os.path.join(self.cookie_dir, 'bilibili_cookies.txt'),
            'youtube': os.path.join(self.cookie_dir, 'youtube_cookies.txt')
        }
    
    def _ensure_whisper_model(self) -> None:
        """确保Whisper模型已加载"""
        if self.whisper_model is None:
            try:
                print("正在加载Whisper模型...")
                self.whisper_model = whisper.load_model("medium")
                print("✅ Whisper模型加载成功")
            except Exception as e:
                print(f"⚠️ Whisper模型加载失败: {str(e)}")

    def _determine_platform(self, url: str) -> Optional[str]:
        """
        确定视频平台
        
        Args:
            url: 视频URL
            
        Returns:
            str: 平台名称 ('youtube', 'douyin', 'bilibili') 或 None
        """
        if 'youtube.com' in url or 'youtu.be' in url:
            return 'youtube'
        elif 'douyin.com' in url:
            return 'douyin'
        elif 'bilibili.com' in url:
            return 'bilibili'
        return None

    def _handle_download_error(self, error: Exception, platform: str, url: str) -> str:
        """
        处理下载错误并返回用户友好的错误消息
        
        Args:
            error: 异常对象
            platform: 平台名称
            url: 视频URL
            
        Returns:
            str: 用户友好的错误消息
        """
        error_msg = str(error)
        
        if "SSL" in error_msg:
            return "⚠️ SSL证书验证失败，请检查网络连接"
        elif "cookies" in error_msg.lower():
            return f"⚠️ {platform}访问被拒绝，可能需要更新cookie或更换IP地址"
        elif "404" in error_msg:
            return "⚠️ 视频不存在或已被删除"
        elif "403" in error_msg:
            return "⚠️ 访问被拒绝，可能需要登录或更换IP地址"
        elif "unavailable" in error_msg.lower():
            return "⚠️ 视频当前不可用，可能是地区限制或版权问题"
        else:
            return f"⚠️ 下载失败: {error_msg}"

    def _get_platform_options(self, platform: str) -> Dict:
        """获取平台特定的下载选项"""
        # 基本选项
        options = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': '%(title)s.%(ext)s'
        }
        
        if platform in self.platform_cookies and os.path.exists(self.platform_cookies[platform]):
            options['cookiefile'] = self.platform_cookies[platform]
            
        return options

    def _validate_cookies(self, platform: str) -> bool:
        """验证cookie是否有效"""
        if platform not in self.platform_cookies:
            return False
        
        cookie_file = self.platform_cookies[platform]
        return os.path.exists(cookie_file)

    def _get_alternative_download_method(self, platform: str, url: str) -> Optional[str]:
        """获取备用下载方法"""
        if platform == 'youtube':
            return 'pytube'
        elif platform == 'douyin':
            return 'requests'
        elif platform == 'bilibili':
            return 'you-get'
        return None

    def _download_with_alternative_method(self, platform: str, url: str, temp_dir: str, method: str) -> Optional[str]:
        """使用备用方法下载"""
        try:
            if method == 'you-get':
                cmd = ['you-get', '--no-proxy', '--no-check-certificate', '-o', temp_dir, url]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    # 查找下载的文件
                    files = [f for f in os.listdir(temp_dir) if f.endswith(('.mp4', '.flv', '.webm'))]
                    if files:
                        return os.path.join(temp_dir, files[0])
                raise Exception(result.stderr)
                
            elif method == 'requests':
                # 使用requests直接下载
                headers = {
                    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 Mobile/15E148 Safari/604.1',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1'
                }
                
                # 首先获取页面内容
                response = httpx.get(url, headers=headers, verify=False)
                
                if response.status_code == 200:
                    # 尝试从页面中提取视频URL
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(response.text, 'html.parser')
                    
                    video_url = None
                    # 查找video标签
                    video_tags = soup.find_all('video')
                    for video in video_tags:
                        src = video.get('src') or video.get('data-src')
                        if src:
                            video_url = src
                            break
                    
                    if not video_url:
                        # 尝试查找其他可能包含视频URL的元素
                        import re
                        video_patterns = [
                            r'https?://[^"\'\s]+\.(?:mp4|m3u8)[^"\'\s]*',
                            r'playAddr":"([^"]+)"',
                            r'play_url":"([^"]+)"'
                        ]
                        for pattern in video_patterns:
                            matches = re.findall(pattern, response.text)
                            if matches:
                                video_url = matches[0]
                                break
                    
                    if video_url:
                        if not video_url.startswith('http'):
                            video_url = 'https:' + video_url if video_url.startswith('//') else video_url
                        
                        # 下载视频
                        video_response = httpx.get(video_url, headers=headers, stream=True, verify=False)
                        if video_response.status_code == 200:
                            file_path = os.path.join(temp_dir, 'video.mp4')
                            with open(file_path, 'wb') as f:
                                for chunk in video_response.iter_content(chunk_size=8192):
                                    if chunk:
                                        f.write(chunk)
                            return file_path
                        
                    raise Exception(f"无法下载视频: HTTP {video_response.status_code}")
                raise Exception(f"无法访问页面: HTTP {response.status_code}")
                
            elif method == 'pytube':
                # 禁用SSL验证
                import ssl
                ssl._create_default_https_context = ssl._create_unverified_context
                
                from pytube import YouTube
                yt = YouTube(url)
                # 获取最高质量的MP4格式视频
                video = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
                if video:
                    return video.download(output_path=temp_dir)
                raise Exception("未找到合适的视频流")
                
        except Exception as e:
            print(f"备用下载方法 {method} 失败: {str(e)}")
            return None

    def _download_video(self, url: str, temp_dir: str) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
        """下载视频并返回音频文件路径和信息"""
        try:
            platform = self._determine_platform(url)
            if not platform:
                raise DownloadError("不支持的视频平台", "unknown", "platform_error")

            # 基本下载选项
            options = {
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                }],
                'quiet': True,
                'no_warnings': True,
            }

            # 下载视频
            for attempt in range(3):  # 最多重试3次
                try:
                    with yt_dlp.YoutubeDL(options) as ydl:
                        print(f"正在尝试下载（第{attempt + 1}次）...")
                        info = ydl.extract_info(url, download=True)
                        if not info:
                            raise DownloadError("无法获取视频信息", platform, "info_error")

                        # 找到下载的音频文件
                        downloaded_files = [f for f in os.listdir(temp_dir) if f.endswith('.mp3')]
                        if not downloaded_files:
                            raise DownloadError("未找到下载的音频文件", platform, "file_error")

                        audio_path = os.path.join(temp_dir, downloaded_files[0])
                        if not os.path.exists(audio_path):
                            raise DownloadError("音频文件不存在", platform, "file_error")

                        video_info = {
                            'title': info.get('title', '未知标题'),
                            'uploader': info.get('uploader', '未知作者'),
                            'description': info.get('description', ''),
                            'duration': info.get('duration', 0),
                            'platform': platform
                        }

                        print(f"✅ {platform}视频下载成功")
                        return audio_path, video_info

                except Exception as e:
                    print(f"⚠️ 下载失败（第{attempt + 1}次）: {str(e)}")
                    if attempt < 2:  # 如果不是最后一次尝试
                        print("等待5秒后重试...")
                        time.sleep(5)
                    else:
                        raise  # 最后一次失败，抛出异常

        except Exception as e:
            error_msg = self._handle_download_error(e, platform, url)
            print(f"⚠️ {error_msg}")
            return None, None

    def _transcribe_audio(self, audio_path: str) -> str:
        """使用Whisper转录音频"""
        try:
            self._ensure_whisper_model()
            if not self.whisper_model:
                raise Exception("Whisper模型未加载")
                
            print("正在转录音频（这可能需要几分钟）...")
            result = self.whisper_model.transcribe(
                audio_path,
                language='zh',  # 指定中文以提高准确性
                task='transcribe',
                best_of=5
            )
            return result["text"].strip()
            
        except Exception as e:
            print(f"⚠️ 音频转录失败: {str(e)}")
            return ""

    def _organize_long_content(self, content: str) -> str:
        """使用AI整理长文内容"""
        if not self.openrouter_available:
            return content

        try:
            # 分段处理长文本
            def split_content(text, max_chars=2000):
                # 按句号分割文本
                sentences = text.split('。')
                chunks = []
                current_chunk = []
                current_length = 0
                
                for sentence in sentences:
                    # 确保句子以句号结尾
                    sentence = sentence.strip() + '。'
                    sentence_length = len(sentence)
                    
                    if current_length + sentence_length > max_chars and current_chunk:
                        # 当前块已满，保存并开始新块
                        chunks.append(''.join(current_chunk))
                        current_chunk = [sentence]
                        current_length = sentence_length
                    else:
                        # 添加句子到当前块
                        current_chunk.append(sentence)
                        current_length += sentence_length
                
                # 添加最后一个块
                if current_chunk:
                    chunks.append(''.join(current_chunk))
                
                return chunks

            # 构建编辑提示词
            system_prompt = """你是一位出版社的资深编辑，有20年的丰富工作资历。你擅长把各种杂乱的资料，理出头绪。
请一步步思考，输出markdown格式的内容，不要输出任何与要求无关的内容，更不要进行总结。
请保持严谨的学术态度，确保输出的内容既专业又易读。

特别注意：
1. 这是一个长文的其中一部分
2. 保持内容的连贯性
3. 不要随意删减重要信息
4. 使用markdown格式组织内容
5. 确保每个要点都得到保留"""

            # 分段处理内容
            content_chunks = split_content(content)
            organized_chunks = []
            
            print(f"内容将分为 {len(content_chunks)} 个部分处理...")
            
            for i, chunk in enumerate(content_chunks, 1):
                print(f"正在处理第 {i}/{len(content_chunks)} 部分...")
                
                # 添加上下文信息
                context = f"这是文章的第 {i}/{len(content_chunks)} 部分。" if len(content_chunks) > 1 else ""
                
                user_prompt = f"""请将以下内容整理成结构清晰的文章片段，要求：
1. 保持原文的核心信息和专业性
2. 使用markdown格式
3. 按照逻辑顺序组织内容
4. 适当添加标题和分段
5. 确保可读性的同时不损失重要信息

{context}

原文内容：

{chunk}"""

                # 调用API
                response = client.chat.completions.create(
                    model=AI_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=0.7,
                    max_tokens=4000
                )
                
                if response.choices:
                    organized_chunk = response.choices[0].message.content.strip()
                    organized_chunks.append(organized_chunk)
                    
            # 合并所有处理后的内容
            final_content = "\n\n".join(organized_chunks)
            
            # 如果有多个部分，再处理一次以确保整体连贯性
            if len(organized_chunks) > 1:
                print("正在优化整体内容连贯性...")
                
                final_prompt = """请检查并优化以下文章的整体连贯性，要求：
1. 确保各部分之间的过渡自然
2. 消除可能的重复内容
3. 统一文章的风格和格式
4. 保持markdown格式
5. 不要删减重要信息

原文内容：

{final_content}"""

                response = client.chat.completions.create(
                    model=AI_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": final_prompt}
                    ],
                    temperature=0.7,
                    max_tokens=4000
                )
                
                if response.choices:
                    final_content = response.choices[0].message.content.strip()
            
            return final_content
                
        except Exception as e:
            print(f"⚠️ 长文整理失败: {str(e)}")
            return content

    def _optimize_content_format(self, content: str) -> Tuple[str, List[str], List[str]]:
        """使用OpenRouter优化内容格式并生成标题"""
        if not self.openrouter_available:
            return content, ["笔记"], []

        try:
            # 构建系统提示词
            system_prompt = """你是一名专注在小红书平台上的写作专家，具有丰富的社交媒体写作背景和市场推广经验。

专业技能：
1. 标题创作技巧：
   - 二极管标题法：
     * 正面刺激：产品/方法 + 即时效果 + 逆天效果
     * 负面刺激：你不xx + 绝对后悔 + 紧迫感
   - 标题要素：
     * 使用惊叹号、省略号增强表达力
     * 采用挑战性和悬念的表述
     * 描述具体成果和效果
     * 融入热点话题和实用工具
     * 必须包含emoji表情

2. 爆款关键词库：
   - 高情感词：绝绝子、宝藏、神器、YYDS、秘方、好用哭了
   - 吸引词：搞钱必看、狠狠搞钱、吐血整理、万万没想到
   - 专业词：建议收藏、划重点、干货、秘籍、指南
   - 情感词：治愈、破防了、泪目、感动、震撼
   - 品质词：高级感、一级棒、无敌了、太绝了

3. 写作风格：
   - 开篇：直击痛点，制造共鸣
   - 语气：热情、亲切、口语化
   - 结构：步骤说明 + 要点总结
   - 段落：每段都要用emoji表情点缀
   - 互动：设置悬念，引导评论
   - 配图：选择高质量、相关性强的图片

4. SEO标签规则：
   - 核心关键词：主题核心词（例：职场、学习、技能）
   - 关联关键词：核心词相关标签（例：职场技巧、学习方法）
   - 高转化词：带购买意向（例：必看、推荐、测评）
   - 热搜词：当前热点（例：AIGC、效率工具）
   - 人群词：目标受众（例：职场人、学生党）

5. 小红书平台特性：
   - 标题控制在20字以内，简短有力
   - 使用emoji增加活力
   - 分段清晰，重点突出
   - 语言接地气，避免过于正式
   - 善用数字、清单形式
   - 突出实用性和可操作性"""

            # 构建用户提示词
            user_prompt = f"""请将以下内容改写成小红书爆款笔记，要求：

1. 标题创作（生成3个）：
   - 必须包含emoji
   - 其中2个标题在20字以内
   - 运用二极管标题法
   - 使用爆款关键词
   - 体现内容核心价值

2. 内容改写：
   - 开篇要吸引眼球
   - 每段都要用emoji装饰
   - 语言要口语化、有趣
   - 适当使用爆款词
   - 突出干货和重点
   - 设置悬念和互动点
   - 结尾要有收束和号召

3. 标签生成：
   - 包含核心关键词
   - 包含热门话题词
   - 包含人群标签
   - 包含价值标签
   - 所有标签都以#开头

原文内容：
{content}

请按以下格式输出：
TITLES
[标题1]
[标题2]
[标题3]

CONTENT
[正文内容]

TAGS
[标签1] [标签2] [标签3] ..."""

            # 调用API
            response = client.chat.completions.create(
                model=AI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
                max_tokens=3000
            )
            
            if response.choices:
                result = response.choices[0].message.content.strip()
                
                # 解析结果
                sections = result.split('\n\n')
                titles = []
                content = ""
                tags = []
                
                current_section = ""
                for section in sections:
                    if section.startswith('TITLES'):
                        current_section = "titles"
                    elif section.startswith('CONTENT'):
                        current_section = "content"
                    elif section.startswith('TAGS'):
                        current_section = "tags"
                    else:
                        if current_section == "titles":
                            if section.strip() and not section.startswith('TITLES'):
                                titles.append(section.strip())
                        elif current_section == "content":
                            if section.strip() and not section.startswith('CONTENT'):
                                content += section.strip() + "\n\n"
                        elif current_section == "tags":
                            if section.strip() and not section.startswith('TAGS'):
                                tags.extend([tag.strip() for tag in section.split() if tag.strip()])
                
                return content.strip(), titles, tags
                
        except Exception as e:
            print(f"⚠️ 内容优化失败: {str(e)}")
            return content, ["笔记"], []

    def _get_unsplash_images(self, query: str, count: int = 3) -> List[Dict[str, str]]:
        """从Unsplash获取相关图片"""
        if not self.unsplash_client:
            print("⚠️ Unsplash客户端未初始化")
            return []
            
        try:
            # 将查询词翻译成英文以获得更好的结果
            if self.openrouter_available:
                try:
                    response = client.chat.completions.create(
                        model=AI_MODEL,
                        messages=[
                            {"role": "system", "content": "你是一个翻译助手，请将中文关键词翻译成英文，只返回翻译结果，不要加任何解释。"},
                            {"role": "user", "content": query}
                        ]
                    )
                    if response.choices:
                        query = response.choices[0].message.content.strip()
                except Exception:
                    pass
            
            # 使用httpx直接调用Unsplash API
            headers = {
                'Authorization': f'Client-ID {os.getenv("UNSPLASH_ACCESS_KEY")}'
            }
            
            response = httpx.get(
                'https://api.unsplash.com/search/photos',
                params={
                    'query': query,
                    'per_page': count,
                    'orientation': 'landscape'
                },
                headers=headers,
                verify=False  # 禁用SSL验证
            )
            
            if response.status_code == 200:
                data = response.json()
                if data['results']:
                    return [photo['urls']['regular'] for photo in data['results']]
            return []
            
        except Exception as e:
            print(f"⚠️ 获取图片失败: {str(e)}")
            return []

    def process_video(self, url: str) -> List[str]:
        """处理视频并生成小红书风格的笔记"""
        print("\n📹 正在处理视频...")
        
        # 创建临时目录
        temp_dir = os.path.join(self.output_dir, 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        
        try:
            # 下载视频
            print("⬇️ 正在下载视频...")
            result = self._download_video(url, temp_dir)
            if not result:
                return []
                
            audio_path, video_info = result
            if not audio_path or not video_info:
                return []
                
            print(f"✅ 视频下载成功: {video_info['title']}")
            
            # 转录音频
            print("\n🎙️ 正在转录音频...")
            print("正在转录音频（这可能需要几分钟）...")
            transcript = self._transcribe_audio(audio_path)
            if not transcript:
                return []

            # 保存原始转录内容
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            original_file = os.path.join(self.output_dir, f"{timestamp}_original.md")
            with open(original_file, 'w', encoding='utf-8') as f:
                f.write(f"# {video_info['title']}\n\n")
                f.write(f"## 视频信息\n")
                f.write(f"- 作者：{video_info['uploader']}\n")
                f.write(f"- 时长：{video_info['duration']}秒\n")
                f.write(f"- 平台：{video_info['platform']}\n")
                f.write(f"- 链接：{url}\n\n")
                f.write(f"## 原始转录内容\n\n")
                f.write(transcript)

            # 整理长文版本
            print("\n📝 正在整理长文版本...")
            organized_content = self._organize_long_content(transcript)
            organized_file = os.path.join(self.output_dir, f"{timestamp}_organized.md")
            with open(organized_file, 'w', encoding='utf-8') as f:
                f.write(f"# {video_info['title']} - 整理版\n\n")
                f.write(f"## 视频信息\n")
                f.write(f"- 作者：{video_info['uploader']}\n")
                f.write(f"- 时长：{video_info['duration']}秒\n")
                f.write(f"- 平台：{video_info['platform']}\n")
                f.write(f"- 链接：{url}\n\n")
                f.write(f"## 内容整理\n\n")
                f.write(organized_content)
            
            # 优化内容格式（小红书版本）
            print("\n✍️ 正在优化内容格式...")
            optimized_content, titles, tags = self._optimize_content_format(organized_content)
            
            # 获取相关图片
            print("\n🖼️ 正在获取配图...")
            images = self._get_unsplash_images(titles[0])
            
            # 生成笔记文件名
            note_file = os.path.join(self.output_dir, f"{timestamp}_1.md")
            
            # 保存笔记
            with open(note_file, 'w', encoding='utf-8') as f:
                f.write(f"# {titles[0]}\n\n")
                
                # 添加视频信息
                f.write(f"## 视频信息\n")
                f.write(f"- 作者：{video_info['uploader']}\n")
                f.write(f"- 时长：{video_info['duration']}秒\n")
                f.write(f"- 平台：{video_info['platform']}\n")
                f.write(f"- 链接：{url}\n\n")
                
                # 添加优化后的内容
                f.write(f"## 笔记内容\n\n")
                f.write(optimized_content)
                
                # 添加图片链接
                if images:
                    f.write("\n\n## 相关图片\n\n")
                    for i, img_url in enumerate(images, 1):
                        f.write(f"![配图{i}]({img_url})\n")
            
            print(f"\n✅ 笔记已保存至: {note_file}")
            print(f"✅ 原始转录内容已保存至: {original_file}")
            print(f"✅ 整理版内容已保存至: {organized_file}")
            return [note_file, original_file, organized_file]
            
        except Exception as e:
            print(f"⚠️ 处理视频时出错: {str(e)}")
            return []
        
        finally:
            # 清理临时文件
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)

if __name__ == "__main__":
    import sys, os, re
    
    if len(sys.argv) != 2:
        print("用法：")
        print("1. 处理单个视频：python video_note_generator.py <视频URL>")
        print("2. 批量处理文件：python video_note_generator.py <文件路径>")
        print("   支持的文件格式：")
        print("   - .txt 文件：每行一个 URL")
        print("   - .md 文件：提取 Markdown 链接中的 URL")
        sys.exit(1)
    
    input_arg = sys.argv[1]
    generator = VideoNoteGenerator()
    
    if os.path.exists(input_arg):
        # 处理文件中的 URLs
        try:
            with open(input_arg, 'r', encoding='utf-8') as f:
                content = f.read()
            
            urls = []
            # 根据文件类型提取 URLs
            if input_arg.endswith('.md'):
                # 从 Markdown 文件中提取 URLs
                # 首先匹配 [text](url) 格式的链接
                md_urls = re.findall(r'\[([^\]]*)\]\((https?://[^\s\)]+)\)', content)
                urls.extend(url for _, url in md_urls)
                
                # 然后匹配裸露的 URLs（不在markdown链接内的URLs）
                # 首先将所有已找到的markdown格式URLs替换为空格
                for _, url in md_urls:
                    content = content.replace(url, '')
                # 现在查找剩余的URLs
                urls.extend(re.findall(r'https?://[^\s\)]+', content))
            else:
                # 从普通文本文件中提取 URLs（每行一个）
                urls = [url.strip() for url in content.splitlines() if url.strip()]
                # 确保每行都是 URL
                urls = [url for url in urls if url.startswith(('http://', 'https://'))]
            
            if not urls:
                print("错误：文件中没有找到有效的 URL")
                sys.exit(1)
            
            # 去重
            urls = list(dict.fromkeys(urls))
            
            print(f"找到 {len(urls)} 个唯一的 URL，开始处理...")
            for i, url in enumerate(urls, 1):
                print(f"\n处理第 {i}/{len(urls)} 个 URL: {url}")
                try:
                    generator.process_video(url)
                except Exception as e:
                    print(f"处理 URL '{url}' 时出错：{str(e)}")
        except Exception as e:
            print(f"读取文件时出错：{str(e)}")
            sys.exit(1)
    else:
        # 检查是否是有效的 URL
        if not input_arg.startswith(('http://', 'https://')):
            print("错误：请输入有效的 URL 或文件路径")
            sys.exit(1)
            
        # 直接处理单个 URL
        try:
            print(f"开始处理 URL: {input_arg}")
            generator.process_video(input_arg)
        except Exception as e:
            print(f"处理 URL 时出错：{str(e)}")
            sys.exit(1)
