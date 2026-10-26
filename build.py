"""把 axis_recorder/ 打成 dist/ 下的 .zip 和 .okscript, 再用 ok-script 自己的检查过一遍。

.zip 里多包一层 axis_recorder/ 文件夹, 和 GitHub Release 的习惯一致; .okscript 是文件直接放在根目录。
"""
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, 'axis_recorder')

sys.path.insert(0, os.path.join(HERE, '..', 'ok-ww'))
from ok.core.script_packager import inspect_script_archive  # noqa: E402

with open(os.path.join(SOURCE, 'manifest.json'), encoding='utf-8') as f:
    manifest = json.load(f)
version = manifest['version']
with open(os.path.join(SOURCE, 'AxisRecorder.py'), encoding='utf-8') as f:
    assert f"PLUGIN_VERSION = '{version}'" in f.read(), 'manifest.json 和 PLUGIN_VERSION 版本号不一致'

files = [name for name in sorted(os.listdir(SOURCE))
         if os.path.isfile(os.path.join(SOURCE, name)) and name.endswith(('.py', '.json'))]
os.makedirs(os.path.join(HERE, 'dist'), exist_ok=True)
for suffix, prefix in (('.zip', manifest['file_name'] + '/'), ('.okscript', '')):
    output = os.path.join(HERE, 'dist', f"{manifest['file_name']}-{version}{suffix}")
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name in files:
            zf.write(os.path.join(SOURCE, name), prefix + name)
    print(output, inspect_script_archive(output))
