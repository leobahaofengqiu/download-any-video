from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, HttpUrl, validator
import yt_dlp
import os
import uuid
import json
import tempfile
from typing import Dict, Optional
import asyncio
import time
import logging
from datetime import datetime
import shutil
from pathlib import Path
import hashlib
import re

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Universal Video Downloader API 🚀",
    description="Railway-optimized video downloader with smart resource management",
    version="2.1.0"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# Security
security = HTTPBearer(auto_error=False)

# Railway Configuration
class Config:
    PORT = int(os.getenv("PORT", 8000))
    API_KEY = os.getenv("API_KEY", "your-secret-api-key-change-this")
    
    # Railway resource limits
    MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 300 * 1024 * 1024))  # 300MB
    MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", 2))
    DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", 600))  # 10 minutes
    
    # Storage
    DOWNLOAD_DIR = "/tmp/downloads"
    CLEANUP_HOURS = 2  # Files deleted after 2 hours

config = Config()

# Global storage
downloads: Dict[str, dict] = {}
active_downloads = set()

# Models
class DownloadRequest(BaseModel):
    url: HttpUrl
    format: str = "mp4"
    quality: str = "720p"
    audio_only: bool = False
    
    @validator('quality')
    def validate_quality(cls, v):
        allowed = ["360p", "480p", "720p", "best"]
        if v not in allowed:
            raise ValueError(f"Quality must be one of: {allowed}")
        return v

class DownloadResponse(BaseModel):
    session_id: str
    status: str
    message: str
    progress: float = 0
    estimated_time: Optional[str] = None

# Utilities
def generate_session_id() -> str:
    return str(uuid.uuid4())[:8]

def sanitize_filename(filename: str) -> str:
    """Clean filename for Railway filesystem"""
    # Remove/replace problematic characters
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    filename = re.sub(r'[^\w\s-.]', '_', filename)
    filename = re.sub(r'[-\s]+', '-', filename)
    return filename[:50]  # Limit length

def get_video_format(quality: str, audio_only: bool) -> str:
    """Get yt-dlp format string"""
    if audio_only:
        return "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio"
    
    quality_map = {
        "360p": "best[height<=360]",
        "480p": "best[height<=480]", 
        "720p": "best[height<=720]",
        "best": "best[height<=720]"  # Cap at 720p for Railway
    }
    
    return quality_map.get(quality, "best[height<=720]")

def check_url_validity(url: str) -> bool:
    """Quick URL validation"""
    supported_domains = [
        'youtube.com', 'youtu.be', 'vimeo.com', 'dailymotion.com',
        'twitter.com', 'x.com', 'instagram.com', 'tiktok.com'
    ]
    
    return any(domain in url.lower() for domain in supported_domains)

# Authentication
async def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="API key required")
    
    if credentials.credentials != config.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    return True

# Progress tracking
def create_progress_hook(session_id: str):
    """Create progress hook for yt-dlp"""
    def hook(d):
        if session_id not in downloads:
            return
            
        try:
            state = downloads[session_id]
            
            if d['status'] == 'downloading':
                # Calculate progress
                if 'total_bytes' in d and d['total_bytes']:
                    progress = (d.get('downloaded_bytes', 0) / d['total_bytes']) * 100
                elif '_percent_str' in d:
                    progress_str = d['_percent_str'].replace('%', '')
                    progress = float(progress_str) if progress_str != 'N/A' else 0
                else:
                    progress = state.get('progress', 0) + 1  # Incremental
                
                # Update state
                state.update({
                    'status': 'downloading',
                    'progress': min(progress, 99),  # Keep at 99 until complete
                    'speed': d.get('speed', 0),
                    'eta': d.get('eta', 0),
                    'updated_at': datetime.now().isoformat()
                })
                
            elif d['status'] == 'finished':
                state.update({
                    'status': 'processing',
                    'progress': 95,
                    'file_path': d['filename'],
                    'updated_at': datetime.now().isoformat()
                })
                
        except Exception as e:
            logger.error(f"Progress hook error for {session_id}: {e}")
    
    return hook

# Download worker
async def download_video(session_id: str, request: DownloadRequest):
    """Main download function"""
    if session_id not in downloads:
        return
    
    state = downloads[session_id]
    temp_dir = None
    
    try:
        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix=f"dl_{session_id}_")
        logger.info(f"Download started: {session_id} in {temp_dir}")
        
        # yt-dlp configuration
        ydl_opts = {
            'format': get_video_format(request.quality, request.audio_only),
            'outtmpl': os.path.join(temp_dir, '%(title).50s.%(ext)s'),
            
            # Railway optimizations
            'retries': 3,
            'socket_timeout': 30,
            'fragment_retries': 3,
            
            # Progress tracking
            'progress_hooks': [create_progress_hook(session_id)],
            
            # Output settings
            'no_warnings': True,
            'quiet': False,
            
            # Post-processors
            'postprocessors': []
        }
        
        # Add audio extraction for audio-only
        if request.audio_only:
            ydl_opts['postprocessors'].append({
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            })
        
        # Start download
        state['status'] = 'downloading'
        start_time = time.time()
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extract info first
            try:
                info = ydl.extract_info(str(request.url), download=False)
            except Exception as e:
                raise Exception(f"Failed to extract video info: {str(e)}")
            
            # Check duration (Railway timeout protection)
            duration = info.get('duration', 0)
            if duration and duration > 3600:  # 1 hour limit
                raise Exception("Video too long (max 60 minutes)")
            
            # Check estimated file size
            filesize = info.get('filesize') or info.get('filesize_approx', 0)
            if filesize and filesize > config.MAX_FILE_SIZE:
                size_mb = filesize / (1024 * 1024)
                raise Exception(f"File too large ({size_mb:.1f}MB, max {config.MAX_FILE_SIZE/(1024*1024):.0f}MB)")
            
            # Store metadata
            state['metadata'] = {
                'title': sanitize_filename(info.get('title', 'Unknown')),
                'uploader': info.get('uploader', 'Unknown'),
                'duration': duration,
                'view_count': info.get('view_count'),
                'upload_date': info.get('upload_date'),
            }
            
            # Download with timeout check
            info = ydl.extract_info(str(request.url), download=True)
            
            # Check timeout
            if time.time() - start_time > config.DOWNLOAD_TIMEOUT:
                raise Exception("Download timeout exceeded")
        
        # Find downloaded file
        downloaded_files = [f for f in os.listdir(temp_dir) if not f.startswith('.')]
        if not downloaded_files:
            raise Exception("No file was downloaded")
        
        downloaded_file = os.path.join(temp_dir, downloaded_files[0])
        
        # Verify file
        if not os.path.exists(downloaded_file):
            raise Exception("Downloaded file not found")
        
        file_size = os.path.getsize(downloaded_file)
        if file_size == 0:
            raise Exception("Downloaded file is empty")
        
        if file_size > config.MAX_FILE_SIZE:
            raise Exception(f"File size exceeds limit: {file_size/(1024*1024):.1f}MB")
        
        # Move to permanent location
        os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
        final_filename = f"{session_id}_{sanitize_filename(downloaded_files[0])}"
        final_path = os.path.join(config.DOWNLOAD_DIR, final_filename)
        
        shutil.move(downloaded_file, final_path)
        
        # Update final state
        state.update({
            'status': 'completed',
            'progress': 100,
            'file_path': final_path,
            'file_size': file_size,
            'filename': final_filename,
            'completed_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat()
        })
        
        logger.info(f"Download completed: {session_id} ({file_size/(1024*1024):.1f}MB)")
        
    except Exception as e:
        logger.error(f"Download failed {session_id}: {str(e)}")
        state.update({
            'status': 'failed',
            'progress': 0,
            'error': str(e)[:200],
            'updated_at': datetime.now().isoformat()
        })
        
    finally:
        # Cleanup temp directory
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except:
                pass
        
        # Remove from active set
        active_downloads.discard(session_id)

# API Endpoints
@app.get("/")
async def root():
    """API Status"""
    return {
        "name": "Universal Video Downloader API",
        "version": "2.1.0",
        "status": "running",
        "railway": True,
        "active_downloads": len(active_downloads),
        "total_sessions": len(downloads),
        "max_concurrent": config.MAX_CONCURRENT,
        "max_file_size_mb": config.MAX_FILE_SIZE / (1024 * 1024)
    }

@app.post("/api/download", response_model=DownloadResponse)
async def create_download(
    request: DownloadRequest,
    background_tasks: BackgroundTasks,
    _: bool = Depends(verify_api_key)
):
    """Start a new download"""
    
    # URL validation
    if not check_url_validity(str(request.url)):
        raise HTTPException(
            status_code=400,
            detail="Unsupported URL. Supported: YouTube, Vimeo, TikTok, Instagram, Twitter"
        )
    
    # Check concurrent limit
    if len(active_downloads) >= config.MAX_CONCURRENT:
        raise HTTPException(
            status_code=429,
            detail=f"Too many active downloads. Max: {config.MAX_CONCURRENT}. Try again later."
        )
    
    # Generate session
    session_id = generate_session_id()
    
    # Initialize state
    downloads[session_id] = {
        'session_id': session_id,
        'status': 'pending',
        'progress': 0,
        'url': str(request.url),
        'format': request.format,
        'quality': request.quality,
        'audio_only': request.audio_only,
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat(),
        'error': None,
        'metadata': {},
        'file_path': None,
        'file_size': 0
    }
    
    # Add to active set
    active_downloads.add(session_id)
    
    # Start download
    background_tasks.add_task(download_video, session_id, request)
    
    # Estimate time
    queue_position = len(active_downloads)
    estimated_time = f"{queue_position * 2-3} minutes" if queue_position > 1 else "2-5 minutes"
    
    logger.info(f"Download queued: {session_id}")
    
    return DownloadResponse(
        session_id=session_id,
        status="pending",
        message="Download started successfully",
        estimated_time=estimated_time
    )

@app.get("/api/status/{session_id}")
async def get_status(session_id: str):
    """Get download status"""
    
    if session_id not in downloads:
        raise HTTPException(status_code=404, detail="Session not found")
    
    state = downloads[session_id]
    
    # Calculate elapsed time
    created_at = datetime.fromisoformat(state['created_at'])
    elapsed = (datetime.now() - created_at).total_seconds()
    
    response = {
        'session_id': session_id,
        'status': state['status'],
        'progress': round(state['progress'], 1),
        'elapsed_seconds': int(elapsed),
        'created_at': state['created_at'],
        'updated_at': state['updated_at']
    }
    
    # Add extra info based on status
    if state['status'] == 'downloading':
        response.update({
            'speed_kbps': round(state.get('speed', 0) / 1024, 1) if state.get('speed') else 0,
            'eta_seconds': state.get('eta', 0)
        })
    
    elif state['status'] == 'completed':
        response.update({
            'file_size_mb': round(state.get('file_size', 0) / (1024 * 1024), 2),
            'filename': state.get('filename'),
            'metadata': state.get('metadata', {})
        })
    
    elif state['status'] == 'failed':
        response['error'] = state.get('error')
    
    return response

@app.get("/api/download/{session_id}")
async def download_file(session_id: str):
    """Download completed file"""
    
    if session_id not in downloads:
        raise HTTPException(status_code=404, detail="Session not found")
    
    state = downloads[session_id]
    
    if state['status'] != 'completed':
        raise HTTPException(
            status_code=400, 
            detail=f"Download not ready. Status: {state['status']}"
        )
    
    file_path = state.get('file_path')
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found or expired")
    
    # Get clean filename
    metadata = state.get('metadata', {})
    title = metadata.get('title', 'video')
    ext = 'mp3' if state.get('audio_only') else state.get('format', 'mp4')
    clean_filename = f"{sanitize_filename(title)}.{ext}"
    
    logger.info(f"File served: {session_id} - {clean_filename}")
    
    return FileResponse(
        path=file_path,
        filename=clean_filename,
        media_type='application/octet-stream'
    )

@app.delete("/api/download/{session_id}")
async def delete_download(session_id: str, _: bool = Depends(verify_api_key)):
    """Cancel/delete download"""
    
    if session_id not in downloads:
        raise HTTPException(status_code=404, detail="Session not found")
    
    state = downloads[session_id]
    
    # Remove file if exists
    file_path = state.get('file_path')
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except:
            pass
    
    # Remove from tracking
    active_downloads.discard(session_id)
    del downloads[session_id]
    
    logger.info(f"Download deleted: {session_id}")
    
    return {"message": "Download deleted successfully"}

@app.get("/api/downloads")
async def list_downloads(_: bool = Depends(verify_api_key)):
    """List all downloads"""
    
    result = []
    for session_id, state in downloads.items():
        result.append({
            'session_id': session_id,
            'status': state['status'],
            'progress': round(state['progress'], 1),
            'created_at': state['created_at'],
            'title': state.get('metadata', {}).get('title', 'Unknown')[:50],
            'file_size_mb': round(state.get('file_size', 0) / (1024 * 1024), 2),
            'format': state.get('format'),
            'audio_only': state.get('audio_only', False)
        })
    
    # Sort by creation time (newest first)
    result.sort(key=lambda x: x['created_at'], reverse=True)
    
    return {
        'downloads': result[:50],  # Limit to 50
        'total': len(result),
        'active': len(active_downloads)
    }

@app.post("/api/cleanup")
async def cleanup_old_files(_: bool = Depends(verify_api_key)):
    """Manual cleanup for old files"""
    
    cleaned = 0
    current_time = datetime.now()
    
    # Clean old downloads from memory
    to_remove = []
    for session_id, state in downloads.items():
        created_at = datetime.fromisoformat(state['created_at'])
        age_hours = (current_time - created_at).total_seconds() / 3600
        
        if age_hours > config.CLEANUP_HOURS:
            # Remove file
            file_path = state.get('file_path')
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    cleaned += 1
                except:
                    pass
            
            to_remove.append(session_id)
    
    # Remove from memory
    for session_id in to_remove:
        downloads.pop(session_id, None)
        active_downloads.discard(session_id)
    
    # Clean orphaned files
    if os.path.exists(config.DOWNLOAD_DIR):
        for file_path in Path(config.DOWNLOAD_DIR).iterdir():
            if file_path.is_file():
                age_hours = (current_time.timestamp() - file_path.stat().st_mtime) / 3600
                if age_hours > config.CLEANUP_HOURS:
                    try:
                        file_path.unlink()
                        cleaned += 1
                    except:
                        pass
    
    logger.info(f"Cleanup completed: {cleaned} items removed")
    
    return {
        'message': f'Cleanup completed',
        'items_removed': cleaned,
        'remaining_sessions': len(downloads)
    }

@app.get("/health")
async def health_check():
    """Health check for Railway"""
    
    # Check disk space
    try:
        disk_usage = shutil.disk_usage(config.DOWNLOAD_DIR)
        disk_free_mb = disk_usage.free / (1024 * 1024)
    except:
        disk_free_mb = 0
    
    return {
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'active_downloads': len(active_downloads),
        'total_sessions': len(downloads),
        'disk_free_mb': int(disk_free_mb),
        'max_concurrent': config.MAX_CONCURRENT,
        'max_file_size_mb': int(config.MAX_FILE_SIZE / (1024 * 1024))
    }

# Startup/Shutdown
@app.on_event("startup")
async def startup():
    """Initialize app"""
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    logger.info("🚀 Video Downloader API started successfully!")
    logger.info(f"📁 Download directory: {config.DOWNLOAD_DIR}")
    logger.info(f"📊 Max file size: {config.MAX_FILE_SIZE/(1024*1024):.0f}MB")
    logger.info(f"⚡ Max concurrent: {config.MAX_CONCURRENT}")

@app.on_event("shutdown") 
async def shutdown():
    """Cleanup on shutdown"""
    logger.info("👋 Shutting down gracefully...")
    active_downloads.clear()
