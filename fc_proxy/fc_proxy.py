# ============================================================
# 阿里云函数计算 FC（Python 3）— GitHub API 代理（v7：新增 action=ai 大模型转发）
# 实测：FC 3.0 Web 函数（内置运行时）event 是 bytes，
#       内容为「JSON 序列化的请求对象」（含 headers/body/httpMethod）。
#       部分环境也可能传 HTTP 报文或 dict，本版全部兼容（JSON 优先，HTTP 兜底）。
# 部署：FC 控制台 → Web 函数（内置运行时 Python 3.10）→ 粘贴本代码
# 环境：无额外依赖（仅标准库）
# v6：白名单开放 images/ 路径；新增 action=upload（图片 base64 直传，不二次编码）
# v7：新增 action=ai（转发大模型请求：provider=deepseek|dashscope，key 由前端传入不落盘）
# ============================================================
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

ALLOWED_OWNER = os.environ.get('GH_OWNER', 'yang6245')
ALLOWED_REPO = os.environ.get('GH_REPO', 'travel-planner')
MAX_BODY_BYTES = int(os.environ.get('MAX_BODY_BYTES', str(10 * 1024 * 1024)))
GITHUB_API = 'https://api.github.com'
RETRIES = 2
TIMEOUT = 30


def handler(event, context):
    """FC 3.0 Web 函数入口：event 兼容 bytes/str/dict 三种形态。"""
    method, headers, body = _parse_event(event)

    # CORS 预检直接放行
    # CORS 预检直接放行：method 为 OPTIONS，或带浏览器预检头
    if method == 'OPTIONS' or headers.get('access-control-request-method'):
        return json_response({'ok': True}, 200)

    # 请求体大小限制
    if len(body) > MAX_BODY_BYTES:
        return json_response({'error': '请求体过大（上限 %dMB）' % (MAX_BODY_BYTES // (1024 * 1024))}, 413)

    # 解析业务 payload；空 body 视为空对象，非法 JSON 返回 400
    try:
        payload = json.loads(body) if isinstance(body, str) and body.strip() else {}
    except (ValueError, TypeError):
        return json_response({'error': '请求体不是合法 JSON'}, 400)
    if not isinstance(payload, dict):
        return json_response({'error': '请求体应为 JSON 对象'}, 400)

    # 鉴权：GitHub Token 由前端经 Header(X-GitHub-Token) 传入
    token = headers.get('x-github-token', '')
    if not token:
        return json_response({'error': '缺少 GitHub Token（请在小程序同步设置中配置）'}, 401)

    # 白名单校验：防止代理被第三方滥用
    owner = payload.get('owner') or ALLOWED_OWNER
    repo = payload.get('repo') or ALLOWED_REPO
    branch = payload.get('branch') or 'main'
    path = payload.get('path', 'data/trip.json')
    if owner != ALLOWED_OWNER or repo != ALLOWED_REPO or not (path.startswith('data/') or path.startswith('images/')):
        return json_response({'error': '目标仓库不在白名单内，已拒绝转发'}, 403)

    action = payload.get('action', 'save')
    if action == 'save':
        return _handle_save(token, owner, repo, branch, path, payload.get('sha') or '', payload.get('content'))
    if action == 'read':
        return _handle_read(token, owner, repo, branch, path)
    if action == 'upload':
        return _handle_upload(token, owner, repo, branch, path, payload.get('content'), payload.get('message'))
    if action == 'ai':
        # 大模型转发（预留）：不涉及仓库白名单，key 由前端传入，不落盘；支持 OpenAI 兼容自定义接口
        return _handle_ai(payload.get('provider') or 'deepseek', payload.get('apiKey') or '',
                          payload.get('model') or '', payload.get('systemPrompt') or '',
                          payload.get('userPrompt') or '', payload.get('baseUrl') or '')
    return json_response({'error': '未知 action: %r' % action}, 400)


def _parse_event(event):
    """把 FC 传入的 event 规整为 (method, headers, body)。

    兼容三种形态：
    1. dict —— FC 事件函数标准结构
    2. bytes/str —— JSON 序列化的请求对象（FC 3.0 Web 函数实测形态）
    3. bytes/str —— 完整 HTTP 请求报文（兜底）
    """
    # 形态 1：dict
    if isinstance(event, dict):
        method = str(event.get('httpMethod') or 'GET').upper()
        headers = {str(k).lower(): v for k, v in (event.get('headers') or {}).items()}
        body = event.get('body') or ''
        return method, headers, _normalize_body(body)

    # 形态 2/3：bytes / str → 先尝试 JSON，再尝试 HTTP 报文
    text = ''
    if isinstance(event, (bytes, bytearray)):
        text = event.decode('utf-8', errors='replace')
    elif isinstance(event, str):
        text = event

    if text.strip():
        # 尝试 JSON 解析（形态 2）
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                headers = {str(k).lower(): v for k, v in (obj.get('headers') or {}).items()}
                body = _normalize_body(obj.get('body'))
                method = str(obj.get('httpMethod') or obj.get('method') or obj.get('http_method')
                             or headers.get('http-method') or headers.get('httpmethod')
                             or headers.get('x-fc-http-method') or 'GET').upper()
                return method, headers, body
        except (ValueError, TypeError):
            pass
        # 兜底：尝试 HTTP 报文解析（形态 3）
        m, _p, h, b = _parse_http_request(text)
        return m, h, _normalize_body(b)

    return 'GET', {}, ''


def _normalize_body(body):
    """把 body 规整为字符串（兼容 dict/list/bytes/None）。"""
    if body is None:
        return ''
    if isinstance(body, (bytes, bytearray)):
        return body.decode('utf-8', errors='replace')
    if isinstance(body, (dict, list)):
        return json.dumps(body, ensure_ascii=False)
    return str(body)


def _parse_http_request(data):
    """解析完整 HTTP 请求报文 → (method, path, headers, body)。"""
    text = data
    if isinstance(data, (bytes, bytearray)):
        text = data.decode('utf-8', errors='replace')
    text = str(text)

    sep = '\r\n\r\n' if '\r\n\r\n' in text else ('\n\n' if '\n\n' in text else None)
    if sep:
        head, body = text.split(sep, 1)
    else:
        head, body = text, ''

    lines = head.replace('\r\n', '\n').split('\n')
    method, path = 'GET', '/'
    if lines and lines[0]:
        parts = lines[0].split(' ')
        if parts:
            method = parts[0].upper()
        if len(parts) > 1:
            path = parts[1]

    headers = {}
    for line in lines[1:]:
        if ':' in line:
            k, v = line.split(':', 1)
            headers[k.strip().lower()] = v.strip()
    return method, path, headers, body


def _handle_save(token, owner, repo, branch, path, sha, content):
    """把行程数据写入 GitHub 仓库（文件已存在则带 sha 更新）。"""
    if not content:
        return json_response({'error': '缺少 content 字段'}, 400)

    content_str = json.dumps(content, ensure_ascii=False)
    b64 = base64.b64encode(content_str.encode('utf-8')).decode('utf-8')

    body_obj = {
        'message': 'update via FC proxy ' + time.strftime('%Y-%m-%d %H:%M:%S'),
        'content': b64,
        'branch': branch,
    }
    if sha:
        body_obj['sha'] = sha

    url = '%s/repos/%s/%s/contents/%s' % (
        GITHUB_API, owner, repo, urllib.parse.quote(path, safe='/:'))
    req = urllib.request.Request(url, method='PUT')
    req.add_header('Authorization', 'Bearer ' + token)
    req.add_header('Accept', 'application/vnd.github.v3+json')
    req.add_header('Content-Type', 'application/json')

    try:
        with _urlopen_with_retry(req, data=json.dumps(body_obj).encode('utf-8')) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return json_response({'ok': True, 'sha': data.get('content', {}).get('sha', '')})
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8', errors='ignore')
        return json_response({'error': 'GitHub 返回 %s: %s' % (e.code, err_body[:300])}, e.code)
    except Exception as e:
        return json_response({'error': '转发失败: %s' % e}, 502)


def _handle_ai(provider, api_key, model, system_prompt, user_prompt, base_url):
    """转发大模型请求（预留）：deepseek|dashscope 快捷，或自定义 OpenAI 兼容接口（baseUrl）。
    key 前端传入不落盘。"""
    if not api_key:
        return json_response({'error': '缺少 API Key'}, 400)
    if not user_prompt:
        return json_response({'error': '缺少 userPrompt'}, 400)
    if provider == 'dashscope':
        url = base_url or 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
        model = model or 'qwen-plus'
    elif provider == 'deepseek':
        url = base_url or 'https://api.deepseek.com/chat/completions'
        model = model or 'deepseek-chat'
    else:
        # 自定义：任何 OpenAI 兼容 /v1/chat/completions 接口（如 Kimi/GLM/混元/硅基流动等）
        url = base_url or ''
        if not url:
            return json_response({'error': '自定义 provider 需要在同步设置填写接口地址（baseUrl）'}, 400)
        if not model:
            return json_response({'error': '自定义 provider 需要填写模型名（model）'}, 400)
    body = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt or ''},
            {'role': 'user', 'content': user_prompt}
        ],
        'temperature': 0.7,
        'max_tokens': 800,
        'stream': False
    }
    req = urllib.request.Request(url, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Authorization', 'Bearer ' + api_key)
    try:
        with _urlopen_with_retry(req, data=json.dumps(body).encode('utf-8')) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        text = (data.get('choices') or [{}])[0].get('message', {}).get('content', '')
        return json_response({'ok': True, 'text': text})
    except urllib.error.HTTPError as e:
        return json_response({'error': 'LLM 返回 %s' % e.code}, e.code)
    except Exception as e:
        return json_response({'error': '转发失败: %s' % e}, 502)


def _handle_upload(token, owner, repo, branch, path, content, message):
    """上传单张图片到 images/ 目录：content 为图片文件字节的 base64（直接透传，不二次编码）。"""
    if not content:
        return json_response({'error': '缺少 content 字段'}, 400)

    body_obj = {
        'message': message or ('upload via FC proxy ' + time.strftime('%Y-%m-%d %H:%M:%S')),
        'content': content,      # 图片 base64 原样透传给 GitHub
        'branch': branch,
    }

    url = '%s/repos/%s/%s/contents/%s' % (
        GITHUB_API, owner, repo, urllib.parse.quote(path, safe='/:'))
    req = urllib.request.Request(url, method='PUT')
    req.add_header('Authorization', 'Bearer ' + token)
    req.add_header('Accept', 'application/vnd.github.v3+json')
    req.add_header('Content-Type', 'application/json')

    try:
        with _urlopen_with_retry(req, data=json.dumps(body_obj).encode('utf-8')) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return json_response({'ok': True, 'sha': data.get('content', {}).get('sha', ''),
                              'path': data.get('content', {}).get('path', '')})
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8', errors='ignore')
        return json_response({'error': 'GitHub 返回 %s: %s' % (e.code, err_body[:300])}, e.code)
    except Exception as e:
        return json_response({'error': '转发失败: %s' % e}, 502)


def _handle_read(token, owner, repo, branch, path):
    """读取 GitHub 文件内容（sha + base64 content），供前端加载行程 / 探测文件。"""
    url = '%s/repos/%s/%s/contents/%s?ref=%s' % (
        GITHUB_API, owner, repo, urllib.parse.quote(path, safe='/:'), urllib.parse.quote(branch))
    req = urllib.request.Request(url)
    req.add_header('Authorization', 'Bearer ' + token)
    req.add_header('Accept', 'application/vnd.github.v3+json')

    try:
        with _urlopen_with_retry(req) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return json_response({
            'ok': True,
            'sha': data.get('sha', ''),
            'size': data.get('size', 0),
            'path': data.get('path', ''),
            'content': data.get('content', ''),   # base64 内容（GitHub 对 >1MB 文件返回空）
            'encoding': data.get('encoding', ''),
        })
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return json_response({'ok': False, 'error': '文件不存在'}, 404)
        return json_response({'error': 'GitHub 返回 %s' % e.code}, e.code)
    except Exception as e:
        return json_response({'error': '转发失败: %s' % e}, 502)


def _urlopen_with_retry(req, data=None, retries=RETRIES, timeout=TIMEOUT):
    """带退避重试的 urlopen：GitHub 5xx/429/网络异常时重试，4xx 直接抛出。"""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return urllib.request.urlopen(req, data=data, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:   # 4xx 不重试
                raise
            last_exc = e
        except Exception as e:                    # 网络层异常（超时/连接失败/SSL）
            last_exc = e
        if attempt < retries:
            time.sleep(0.5 * (attempt + 1))       # 0.5s / 1s 退避
    raise last_exc


def json_response(obj, status=200):
    """FC 3.0 要求的返回结构（dict，含 statusCode/headers/body）。"""
    return {
        'statusCode': status,
        'headers': {
            'Content-Type': 'application/json; charset=utf-8',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Content-Type, X-GitHub-Token',
        'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
        'Access-Control-Max-Age': '86400',     # 缓存预检结果（Chrome 上限 7200s / Firefox 86400s，浏览器自动截断），省掉重复的 OPTIONS 调用（每次保存原本 = OPTIONS + POST 两次调用）
        'Cache-Control': 'no-store',           # 禁止缓存，防止敏感数据落缓存
            'X-Content-Type-Options': 'nosniff',   # 防 MIME 嗅探
        },
        'body': json.dumps(obj, ensure_ascii=False),
    }
