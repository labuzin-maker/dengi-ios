СБОРКА БЕЗ XCODE (через GitHub) → установка через AltServer

1. Создайте новый публичный репозиторий на github.com (например dengi-ios).
2. Распакуйте архив. Загрузите ВСЁ содержимое в репозиторий: Add file → Upload files, перетащите все папки и файлы
   (папки .github, ios, www и файлы package.json, package-lock.json, capacitor.config.json).
   Если папка .github не перетащилась: Add file → Create new file, имя  .github/workflows/ios.yml , вставьте текст из файла ios.yml.
3. Вкладка Actions → слева «Build iOS (unsigned IPA)» → Run workflow. Сборка идёт 5–10 минут.
4. Когда появится зелёная галочка: откройте запуск → внизу Artifacts → Dengi-ipa. Скачайте, распакуйте — внутри Dengi.ipa.
5. AltServer (в трее) → значок → Sideload .ipa… → выберите Dengi.ipa → iPhone должен быть подключён кабелем (или в одной Wi-Fi сети с включённой синхронизацией) → введите Apple ID.
6. iPhone: Настройки → Основные → VPN и управление устройством → доверять Apple ID. Включить «Режим разработчика» (Конфиденциальность и безопасность).
Если сборка красная — пришлите мне текст ошибки из лога.
