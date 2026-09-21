# 修复 6: HTTP 请求体流式读取

## 文件: `src/agent_manager/application/http.py`

**位置**: Lines 209-259 (大文件上传处理)

### 步骤 1: 添加流式读取方法

在 `ManagerServer` 类中添加：

```python
def _read_request_body_streaming(self, max_size: int = None) -> bytes:
    """
    流式读取请求体，防止内存耗尽
    
    Args:
        max_size: 最大允许大小（字节）
    
    Returns:
        请求体字节数据
    
    Raises:
        ValueError: 请求体过大
    """
    content_length = int(self.headers.get('Content-Length', 0))
    
    if max_size is None:
        max_size = MAX_IMPORT_BODY_BYTES
    
    if content_length > max_size:
        raise ValueError(f"Request body too large: {content_length} bytes (max: {max_size})")
    
    # 分块读取
    CHUNK_SIZE = 65536  # 64KB chunks
    chunks = []
    total_read = 0
    
    while total_read < content_length:
        chunk_size = min(CHUNK_SIZE, content_length - total_read)
        chunk = self.rfile.read(chunk_size)
        
        if not chunk:
            break
        
        total_read += len(chunk)
        
        # 双重检查：防止Content-Length说谎
        if total_read > max_size:
            raise ValueError(f"Request body exceeded maximum size during read: {max_size} bytes")
        
        chunks.append(chunk)
    
    return b''.join(chunks)
```

### 步骤 2: 修改 do_POST 方法

查找现有的 `do_POST` 方法中读取请求体的代码：

**修改前**:
```python
def do_POST(self):
    content_length = int(self.headers.get('Content-Length', 0))
    body = self.rfile.read(content_length)  # 一次性读入内存
    # 处理 body
```

**修改后**:
```python
def do_POST(self):
    """处理 POST 请求（使用流式读取）"""
    try:
        # 使用流式读取替代一次性读取
        body = self._read_request_body_streaming()
        
        # 继续原有逻辑处理 body
        # ... 原有的处理代码 ...
        
    except ValueError as e:
        self.send_response(413)  # Payload Too Large
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"error": str(e)}).encode())
        return
```

**注意**: 需要保留原有 do_POST 中的所有其他逻辑，只替换读取请求体的部分。

**验证命令**:
```bash
python -m pytest tests/integration/test_export_integration.py -v
```
