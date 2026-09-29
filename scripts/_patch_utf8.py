"""修 PowerShell 输出编码：管道输出默认 GBK，中文路径 utf-8 解码变乱码导致 lnk 匹配失败。"""
import pathlib

p = pathlib.Path('src/screen_agent/tools/apps.py')
s = p.read_text(encoding='utf-8')

enc_prefix = "$OutputEncoding = [Console].OutputEncoding = [Text.Encoding]::UTF8;"

# lnk 批量解析脚本加 UTF-8 前缀
old1 = '            "$ErrorActionPreference=\'SilentlyContinue\';"'
new1 = ('            "$ErrorActionPreference=\'SilentlyContinue\';'
        + enc_prefix + '"')
assert old1 in s, 'lnk script prefix anchor not found'
s = s.replace(old1, new1, 1)

# Get-StartApps 脚本同前缀
old2 = "                 "Get-StartApps | ConvertTo-Json -Compress"],"
new2 = ("                 '" + enc_prefix
        + " Get-StartApps | ConvertTo-Json -Compress'],")
assert old2 in s, 'uwp script anchor not found'
s = s.replace(old2, new2)

p.write_text(s, encoding='utf-8')
print('utf8 fix ok')
