# Статус реализации

Актуализировано 23.10.2026. На st1 установлен **0.7.0-alpha**, runtime `ff62cf4d1fddd1ed97ffe5cab7d2915ec822fab4`. Прежние результаты сохранены в истории; текущая поставка добавляет согласованный ASR backend PC2.

Точный установленный SHA: `ff62cf4d1fddd1ed97ffe5cab7d2915ec822fab4`. Все3 контейнера healthy/restart0/OOMfalse, schema2 и приватные mounts сохранены. [GitHub39883422671](https://github.com/nd4y/phonoteka/actions/runs/39883422671):639backend/210frontend,63 installed modules,19ASR/7combined/15ordinary; postflight42target/19origin/63Grafana. PC2 CUDA фактически проверен двумя полными треками через установленную очередь, без экспорта исходников и записи таймкодов в аудио. Подробности - CHECK-049/050 [реестра](requirements-register.md) и [поставка PC2](pc2-asr-delivery-2026-10-23.md).

**Полное ТЗ и Production Ready пока не подтверждены.** [Ревью готовности](production-readiness.md) отдельно перечисляет найденные дефекты, уже выпущенные исправления, точные проверки и открытые условия приемки. Наличие кода, работающего контейнера или технического succeeded не подменяет приемку продукта и качество музыки. Разрешенная владельцем [проверка Edge](browser-acceptance-2026-10-22.md) завершена в описанном объеме; запуск PWA отдельным приложением подтвержден владельцем.

| Область | Реализовано и проверено | Открытая граница |
| --- | --- | --- |
| Безопасность | Single owner, Argon2, server sessions/idle/absolute expiry, CSRF/origin, scoped revocable tokens, операции изменения данных с authorization; security pins проверены в образах; logout/cache/session проверены в Edge на изолированном демо с теми же UI-файлами | Физическая проверка Android остается отдельной приемкой; браузерное демо не считается проверкой телефона |
| Каталог/форматы | Content signatures, Unicode, SHA/revisions, native MP3/FLAC/MP4/Vorbis/Opus/WAV/AIFF, readonly/invalid profiles, CUE и PCM-verified derivatives | Поврежденные и неизвестные файлы остаются с причинами; нет обещания восстановить произвольное повреждение |
| Очереди | Durable bounded async/thread workers, independent inference, pause/cancel/resume/restart/idempotency, CAS, storage/provider waits; live reconnect и устаревшие ответы исправлены и проверены | В Edge подтверждены реальные live actions/logs и безопасная отмена одного аудита; числовой ETA/reconnect fault cases проверены автоматически, а не этой одиночной браузерной попыткой |
| Провайдеры | MusicBrainz/LRCLIB/CAA/local/ShazamIO, согласия/права/TTL/cache/budgets/retry, YouTube provenance и owner confirmation | No match и поисковый кандидат не доказывают идентичность; keyed providers выключены без настройки |
| ASR/ALIGN | Pinned PC2 Large-v3 по HTTPS, отдельный supervisor/лимиты/отмена; CPU small/int8 как явный3GiB профиль. Реальные полные RU/EN:173/224слова,9/42 и14/56 DTW-строк; NAS child peak105MiB | Частичные черновики с обязательной проверкой; музыкальная приемка30 треков не пройдена, автоматический quality gate выключен |
| Запись/откат | Проверенные backups, packet identity, native reopen/full decode, journals/atomic replace/CAS; реальные ID3v1/once дефекты исправлены | Каждая запись требует подходящего профиля/прав/проверок, неисправное аудио не пропускается через ослабленный guard |
| Ready/доставка | Version-bound review, план/hash/CAS, manifests/M3U8/sidecars/leases/receipts, восстановление и recovery gate; согласованный offline Poweramp1031/Android16 эмулятор:20 generated MP3/FLAC, lyrics/art/tags, ReplayGain/M3U8 проверены | Музыкальная точность, реальный альбом gapless и доставка обновления поверх прежнего имени отдельно не подтверждены |
| UI/PWA/player | В Edge проверены обычный/CUE плеер, Back/dirty/reload, обе темы и согласованные 390/768/1024/1440 px; public-only service worker, controlled-origin outage/reconnect/logout; владелец подтвердил standalone PWA | Проверка Poweramp в эмуляторе описана отдельно; она не подтверждает акустический выход физического устройства |
| Эксплуатация | Matched GitHub images, controlled schema2 migration/checkpoint/deploy, NAS isolation, metrics/stdout/Grafana; bounded processing journal | Журнал P1 - частичный. OTel SDK/OTLP/Collector не развернуты и не считаются реализованными |

Снимок коллекции: 2142 аудио + 11 CUE, 525 файлов со встроенным LRC, 700 с обложкой, 1971 запись с отсутствующими данными/ошибками. Отчет владельца и независимая проверка качества отделены от технической готовности приложения.

Облачные источники и внешние ASR-адаптеры спроектированы, не реализованы. Новые функции без отдельного согласования не добавляются. Исторические стадии и неуспешные проверки сохранены в acceptance-audit/requirements-register и приватных протоколах, а не выдаются за текущее состояние.


## CHG-044: проверка до поставки

[Мастер/справка/отчет f41bba9](collection-guide-review.md) реализованы в согласованном объеме. Build-01:210frontend/105backend, production/PWA build и audit0. Edge: новые четыре responsive размера, drilldown, shared Back/F5, controlled origin502/reconnect и logout с playing WAV. Это source-overlay демо, не приемка производственного OCI. [ASR разбор](asr-zero-pilot-review.md) объясняет0/66 без новых jobs/модели/записи. Выпуск прошел полную matched CI и проверку доставки; подробный операционный журнал сохранен отдельно.


## CHG-044: выпуск проверен

Справка, мастер организации, понятный отчет и исправления PWA/Back/F5/logout реализованы и выпущены. [CI39798349961](https://github.com/nd4y/phonoteka/actions/runs/39798349961) для f41bba9 прошел:613backend/210frontend,19ASR/7combined/15ordinary. Проверки установленной версии через HTTPS пройдены; подробный журнал развертывания хранится отдельно от репозитория. Физическая проверка Android/Poweramp и независимая музыкальная приемка остаются открытыми.
