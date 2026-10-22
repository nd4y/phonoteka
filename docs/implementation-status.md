# Статус реализации

Актуализировано 22.10.2026. На st1 установлен **0.7.0-alpha**, runtime `f41bba90afe174fa2e386032833839ff7bd4b579`. Прежние сведения о production 0.6 и неразвернутом кандидате больше не описывают текущую установку.

Точный установленный SHA: `f41bba90afe174fa2e386032833839ff7bd4b579`. Все три контейнера повторно проверены healthy, restart 0 / OOM false. Схема 2 и приватные mounts сохранены. [GitHub 39798349961](https://github.com/nd4y/phonoteka/actions/runs/39798349961) проверил 613 backend / 210 frontend tests и 19 ASR / 7 combined / 15 ordinary критериев; postflight проверил 42 target / 19 origin checks и 63 Grafana queries. Подробности - CHECK-041..042 [реестра](requirements-register.md).

**Полное ТЗ и Production Ready пока не подтверждены.** [Ревью готовности](production-readiness.md) отдельно перечисляет найденные дефекты, уже выпущенные исправления, точные проверки и открытые условия приемки. Наличие кода, работающего контейнера или технического succeeded не подменяет приемку продукта и качество музыки. Разрешенная владельцем [проверка Edge](browser-acceptance-2026-10-22.md) завершена в описанном объеме; запуск PWA отдельным приложением подтвержден владельцем.

| Область | Реализовано и проверено | Открытая граница |
| --- | --- | --- |
| Безопасность | Single owner, Argon2, server sessions/idle/absolute expiry, CSRF/origin, scoped revocable tokens, операции изменения данных с authorization; Pillow 13.0.0 / certifi 2026.10.19 фактически проверены в трех установленных контейнерах | Реальный logout/cache/session протокол используемого Edge-сеанса не выполнялся; автоматические и серверные проверки пройдены отдельно |
| Каталог/форматы | Content signatures, Unicode, SHA/revisions, native MP3/FLAC/MP4/Vorbis/Opus/WAV/AIFF, readonly/invalid profiles, CUE и PCM-verified derivatives | Поврежденные и неизвестные файлы остаются с причинами; нет обещания восстановить произвольное повреждение |
| Очереди | Durable bounded async/thread workers, independent inference, pause/cancel/resume/restart/idempotency, CAS, storage/provider waits; live reconnect и устаревшие ответы исправлены и проверены | В Edge подтверждены реальные live actions/logs и безопасная отмена одного аудита; числовой ETA/reconnect fault cases проверены автоматически, а не этой одиночной браузерной попыткой |
| Провайдеры | MusicBrainz/LRCLIB/CAA/local/ShazamIO, согласия/права/TTL/cache/budgets/retry, YouTube provenance и owner confirmation | No match и поисковый кандидат не доказывают идентичность; keyed providers выключены без настройки |
| ASR/ALIGN | Отдельный pinned CPU process/container, лимиты памяти/времени/PCM/result, durable jobs, независимый текст и DTW | Музыкальная приемка не пройдена: последний разрешенный пилот 0/66 строк; только черновики, автоматический quality gate выключен |
| Запись/откат | Проверенные backups, packet identity, native reopen/full decode, journals/atomic replace/CAS; реальные ID3v1/once дефекты исправлены | Каждая запись требует подходящего профиля/прав/проверок, неисправное аудио не пропускается через ослабленный guard |
| Ready/доставка | Version-bound review, план/hash/CAS, manifests/M3U8/sidecars/leases/receipts, восстановление и recovery gate | Телефонный offline Poweramp, подтверждение слов/таймкодов и итогового пакета отложены владельцем |
| UI/PWA/player | В Edge проверены обычный/CUE плеер, Back/dirty/reload, обе темы и согласованные 390/768/1024/1440 px; public-only service worker, анимации/reduced motion; владелец подтвердил standalone PWA | Реальный browser offline/reconnect и физический Android/Poweramp остаются открытыми; viewport testing не считается тестом телефона |
| Эксплуатация | Matched GitHub images, controlled schema2 migration/checkpoint/deploy, NAS isolation, metrics/stdout/Grafana; bounded processing journal | Журнал P1 - частичный. OTel SDK/OTLP/Collector не развернуты и не считаются реализованными |

Снимок коллекции: 2142 аудио + 11 CUE, 525 файлов со встроенным LRC, 700 с обложкой, 1971 запись с отсутствующими данными/ошибками. Отчет владельца и независимая проверка качества отделены от технической готовности приложения.

Облачные источники и внешние ASR-адаптеры спроектированы, не реализованы. Новые функции без отдельного согласования не добавляются. Исторические стадии и неуспешные проверки сохранены в acceptance-audit/requirements-register и приватных протоколах, а не выдаются за текущее состояние.


## CHG-044: проверенный кандидат, поставка ожидается

[Мастер/справка/отчет f41bba9](collection-guide-review.md) реализованы в согласованном объеме. Build-01:210frontend/105backend, production/PWA build и audit0. Edge: новые четыре responsive размера, drilldown, shared Back/F5, controlled origin502/reconnect и logout с playing WAV. Это source-overlay демо, не приемка производственного OCI. [ASR разбор](asr-zero-pilot-review.md) объясняет0/66 без новых jobs/модели/записи. Выпуск прошел полную matched CI и проверку доставки; подробный операционный журнал сохранен отдельно.


## CHG-044: выпуск проверен

Справка, мастер организации, понятный отчет и исправления PWA/Back/F5/logout реализованы и выпущены. [CI39798349961](https://github.com/nd4y/phonoteka/actions/runs/39798349961) для f41bba9 прошел:613backend/210frontend,19ASR/7combined/15ordinary. Проверки установленной версии через HTTPS пройдены; подробный журнал развертывания хранится отдельно от репозитория. Физическая проверка Android/Poweramp и независимая музыкальная приемка остаются открытыми.
