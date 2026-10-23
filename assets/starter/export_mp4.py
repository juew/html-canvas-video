#!/usr/bin/env python3
"""本地 Canvas → MP4。依赖 Chrome/Chromium、FFmpeg；Python 3.9+ 仅用标准库。"""
import argparse
import http.server
import io
import json
import math
import mimetypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import wave

ROOT = Path(__file__).resolve().parent


def find_chrome():
    for name in ('google-chrome', 'chromium', 'chromium-browser'):
        if path := shutil.which(name):
            return path
    for path in (Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
                 Path(os.environ.get('PROGRAMFILES', '')) / 'Google/Chrome/Application/chrome.exe'):
        if path.is_file():
            return str(path)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'video.mp4')
    parser.add_argument('--size', help='输出宽x高；省略使用 HTML 的 VIDEO 配置')
    parser.add_argument('--fps', type=int, help='帧率；省略使用 HTML 的 VIDEO 配置')
    parser.add_argument('--chrome', default=find_chrome())
    parser.add_argument('--timeout', type=float, default=120, help='无有效进展的停滞超时秒数，默认 120；不限制总时长')
    parser.add_argument('--stills', help='仅导出指定秒数的 JPEG，例如 0,1.25,3；不生成音轨或 MP4')
    args = parser.parse_args()
    output = args.out.resolve()
    still_times = None
    if args.stills is not None:
        try:
            still_times = [float(t) for t in args.stills.split(',')]
            if not still_times or any(not math.isfinite(t) or t < 0 for t in still_times):
                raise ValueError
        except ValueError:
            parser.error('--stills 使用逗号分隔的非负秒数')
    still_paths = [ROOT / 'stills' / f'still_{t:07.2f}.jpg' for t in still_times or []]
    if len(set(still_paths)) != len(still_paths):
        parser.error('静帧文件名保留两位小数，请勿指定会得到同名文件的时间')
    if any(path.exists() for path in still_paths):
        parser.error('指定静帧文件已存在，请移动旧文件或选择其他时间')
    if still_times is None and output.exists():
        parser.error(f'文件已存在，请换一个输出名：{output}')
    if still_times is None and (not output.parent.is_dir() or output.suffix.lower() != '.mp4'):
        parser.error('输出必须为已有目录中的 .mp4 文件')
    if not args.chrome or not Path(args.chrome).is_file():
        parser.error('需要本机 Chrome/Chromium；浏览器可用 --chrome 指定')
    if still_times is None and not shutil.which('ffmpeg'):
        parser.error('MP4 导出需要本机 ffmpeg')
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout 必须是正数')
    query = {'export': '1'}
    if still_times is not None:
        query['stills'] = ','.join(map(str, still_times))
    if args.size:
        try:
            width, height = map(int, args.size.lower().split('x'))
            if any(n <= 0 or n % 2 for n in (width, height)):
                raise ValueError
            query.update(width=width, height=height)
        except ValueError:
            parser.error('--size 使用正偶数，例如 640x360')
    if args.fps is not None:
        if not 1 <= args.fps <= 120:
            parser.error('--fps 支持 1–120 的整数')
        query['fps'] = args.fps

    with tempfile.TemporaryDirectory(prefix='.export-', dir=ROOT) as work:
        work = Path(work)
        started = time.monotonic()
        state = {'meta': None, 'ff': None, 'frames': 0, 'error': None, 'done': threading.Event(),
                 'last_progress': started, 'progress': '等待页面开始', 'frame_started': None}
        ff_log = (work / 'ffmpeg.log').open('w+b')
        chrome_log = (work / 'chrome.log').open('w+b')
        encoded = work / 'encoded.mp4'

        def progress(label):
            state['progress'] = label
            state['last_progress'] = time.monotonic()

        def report_frames():
            count, total = state['frames'], state['expected']
            elapsed = max(.001, time.monotonic() - state['frame_started'])
            rate = count / elapsed
            minutes, seconds = divmod(round((total - count) / rate), 60)
            print(f'进度 {count}/{total} 帧 ({count / total:.1%}) | {rate:.1f} fps | '
                  f'预计剩余 {minutes} 分 {seconds} 秒', flush=True)

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def reply(self, code, body=b''):
                self.send_response(code)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                name = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).lstrip('/') or 'index.html'
                path = (ROOT / name).resolve()
                if not path.is_relative_to(ROOT) or any(p.startswith('.') for p in Path(name).parts) or not path.is_file():
                    return self.reply(404)
                self.send_response(200)
                self.send_header('Content-Type', mimetypes.guess_type(path)[0] or 'application/octet-stream')
                self.send_header('Content-Length', str(path.stat().st_size))
                self.end_headers()
                with path.open('rb') as handle:
                    shutil.copyfileobj(handle, self.wfile)

            def do_POST(self):
                if self.headers.get('Origin') != origin:
                    return self.reply(403, b'Only this local page may upload')
                route = urllib.parse.urlsplit(self.path)
                if route.path not in ('/start', '/audio', '/frame', '/done', '/error'):
                    return self.reply(404)
                try:
                    meta = state['meta']
                    limit = 16384
                    if meta and route.path == '/audio':
                        limit = 44 + math.ceil(meta['duration'] * 48000) * meta.get('channels', 0) * 2
                    if meta and route.path == '/frame':
                        limit = meta['width'] * meta['height'] * 4 + 1048576
                    length = int(self.headers.get('Content-Length', '-1'))
                    if not 0 <= length <= limit:
                        raise ValueError('上传长度超出本片需要的范围')
                    self.connection.settimeout(min(30, args.timeout))
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise ValueError('上传未完成')
                    if route.path == '/start':
                        if meta:
                            raise ValueError('重复开始导出')
                        meta = json.loads(body)
                        for key in ('width', 'height', 'fps'):
                            if type(meta[key]) is not int or meta[key] <= 0:
                                raise ValueError(f'{key} 必须为正整数')
                        if meta['width'] % 2 or meta['height'] % 2 or meta['fps'] > 120:
                            raise ValueError('H.264 需要偶数宽高，帧率上限为 120')
                        duration = meta['duration']
                        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
                            raise ValueError('时长必须是有限正数')
                        if still_times is None:
                            if type(meta.get('channels')) is not int or meta['channels'] not in (1, 2):
                                raise ValueError('音轨 channels 必须为 1 或 2')
                        else:
                            if any(t > duration for t in still_times):
                                raise ValueError(f'静帧时间须在 0–{duration} 秒之间')
                            if any(path.exists() for path in still_paths):
                                raise ValueError('静帧文件已存在，拒绝覆盖')
                            (ROOT / 'stills').mkdir(exist_ok=True)
                        state['meta'] = meta
                        state['expected'] = len(still_times) if still_times is not None else math.ceil(duration * meta['fps'])
                        progress('已接收影片参数')
                    elif route.path == '/audio':
                        if still_times is not None or not meta or state['ff']:
                            raise ValueError('音轨上传顺序错误')
                        with wave.open(io.BytesIO(body), 'rb') as wav:
                            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) != (meta['channels'], 2, 48000, math.ceil(meta['duration'] * 48000)):
                                raise ValueError(f'音轨应为等长的 48kHz、{meta["channels"]} 声道 16-bit WAV')
                        (work / 'audio.wav').write_bytes(body)
                        state['ff'] = subprocess.Popen([
                            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-n',
                            '-f', 'image2pipe', '-framerate', str(meta['fps']), '-c:v', 'mjpeg', '-i', 'pipe:0',
                            '-i', str(work / 'audio.wav'), '-map', '0:v', '-map', '1:a',
                            '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p',
                            '-c:a', 'aac', '-b:a', '128k', '-t', str(meta['duration']),
                            '-movflags', '+faststart', str(encoded)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=ff_log)
                        progress('已接收音轨')
                    elif route.path == '/frame':
                        index = int(urllib.parse.parse_qs(route.query).get('i', ['-1'])[0])
                        if not meta or (still_times is None and not state['ff']) or index != state['frames'] or index >= state['expected']:
                            raise ValueError('帧上传顺序或数量错误')
                        if not body.startswith(b'\xff\xd8') or not body.endswith(b'\xff\xd9'):
                            raise ValueError('画面不是完整 JPEG')
                        if state['frame_started'] is None:
                            state['frame_started'] = time.monotonic()
                        if still_times is not None:
                            with still_paths[index].open('xb') as destination:
                                destination.write(body)
                        else:
                            state['ff'].stdin.write(body)
                            state['ff'].stdin.flush()
                        state['frames'] += 1
                        progress(f'已接收 {state["frames"]}/{state["expected"]} 帧')
                        if state['frames'] % (meta['fps'] * 2) == 0 or state['frames'] == state['expected']:
                            report_frames()
                    elif route.path == '/done':
                        if not meta or (still_times is None and not state['ff']) or state['frames'] != state['expected']:
                            raise ValueError('帧未全部到达，不能完成导出')
                        state['done'].set()
                    else:
                        raise ValueError(body.decode('utf-8', 'replace'))
                    self.reply(204)
                except Exception as error:
                    state['error'] = str(error)
                    state['done'].set()
                    try:
                        self.reply(400, str(error).encode('utf-8'))
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        server = chrome = None
        try:
            # Daemon request threads let a stalled upload exit after its encoder is stopped.
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            origin = f'http://127.0.0.1:{server.server_port}'
            threading.Thread(target=server.serve_forever, daemon=True).start()
            chrome = subprocess.Popen([
                args.chrome, '--headless=new', '--no-first-run', '--no-default-browser-check', '--mute-audio',
                f'--user-data-dir={work / "profile"}', f'{origin}/index.html?{urllib.parse.urlencode(query)}'],
                stdout=subprocess.DEVNULL, stderr=chrome_log)
            while not state['done'].wait(.2):
                if chrome.poll() is not None:
                    raise RuntimeError('Chrome 在导出结束前退出。若执行沙箱限制 Chrome，请使用允许启动 Chrome 的本地终端执行')
                stalled = time.monotonic() - state['last_progress']
                if stalled > args.timeout:
                    raise TimeoutError(f'导出停滞 {stalled:.1f} 秒；最后进度：{state["progress"]}；'
                                       '检查页面或编码器，必要时用 --timeout 调整停滞阈值')
            if state['error']:
                raise RuntimeError(state['error'])
            if still_times is not None:
                print(f'完成：{ROOT / "stills"}；{state["frames"]} 张静帧', flush=True)
                return
            state['ff'].stdin.close()
            try:
                code = state['ff'].wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                stalled = time.monotonic() - state['last_progress']
                raise TimeoutError(f'编码收尾停滞 {stalled:.1f} 秒；最后进度：{state["progress"]}') from None
            if code:
                raise RuntimeError(f'FFmpeg 退出码 {code}')
            if not encoded.is_file() or encoded.stat().st_size == 0:
                raise RuntimeError('编码未生成成片')
            # Exclusive creation also protects a file created while the render was running.
            with output.open('xb') as destination:
                try:
                    with encoded.open('rb') as source:
                        shutil.copyfileobj(source, destination)
                except BaseException:
                    output.unlink(missing_ok=True)
                    raise
            print(f'完成：{output}；{state["frames"]} 帧，{time.monotonic() - started:.1f} 秒', flush=True)
        except BaseException:
            for name, handle in (('chrome.log', chrome_log), ('ffmpeg.log', ff_log)):
                handle.flush()
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 3000))
                tail = handle.read().decode('utf-8', 'replace')
                print(f'--- {name} 最后 3000 字节 ---\n{tail or "（空）"}', file=sys.stderr, flush=True)
            raise
        finally:
            # Stop the pipe reader before waiting for HTTP shutdown; write() may be blocked.
            if state['ff'] and state['ff'].poll() is None:
                state['ff'].kill(); state['ff'].wait()
            if chrome and chrome.poll() is None:
                chrome.terminate()
                try:
                    chrome.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    chrome.kill(); chrome.wait()
            if server:
                server.shutdown(); server.server_close()
            ff_log.close(); chrome_log.close()


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        sys.exit(f'导出失败：{error or "已中断"}')
