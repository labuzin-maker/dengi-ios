import re, shutil, subprocess
p = 'ios/App/App/Info.plist'
s = open(p, encoding='utf-8').read()
add = '''	<key>NSMicrophoneUsageDescription</key>
	<string>Микрофон нужен, чтобы записывать расходы голосом.</string>
	<key>NSSpeechRecognitionUsageDescription</key>
	<string>Распознавание речи нужно, чтобы превратить сказанное в запись о расходе.</string>
	<key>NSCameraUsageDescription</key>
	<string>Камера нужна, чтобы сканировать QR-код чека.</string>
	<key>UIViewControllerBasedStatusBarAppearance</key>'''
assert '\t<key>UIViewControllerBasedStatusBarAppearance</key>' in s
s = s.replace('\t<key>UIViewControllerBasedStatusBarAppearance</key>', add, 1)
s = re.sub(r'(<key>UISupportedInterfaceOrientations</key>\s*<array>)\s*<string>UIInterfaceOrientationPortrait</string>.*?(</array>)',
           r'\1\n\t\t<string>UIInterfaceOrientationPortrait</string>\n\t\2', s, count=1, flags=re.S)
s = re.sub(r'(<key>CFBundleDisplayName</key>\s*<string>)[^<]*', r'\1Деньги', s)
open(p, 'w', encoding='utf-8').write(s)
icon = 'ios/App/App/Assets.xcassets/AppIcon.appiconset/AppIcon-512@2x.png'
if shutil.which('sips'):
    subprocess.check_call(['sips', '-s', 'format', 'png', '-z', '1024', '1024', 'icon.png', '--out', icon])
else:
    from PIL import Image
    Image.open('icon.png').convert('RGB').resize((1024, 1024), Image.LANCZOS).save(icon)
print('patched')
