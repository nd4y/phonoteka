# Статус реализации

Актуализировано 22.10.2026. На st1 установлен **0.7.0-alpha**, runtime `bb98d3e42534e59583237fb07237cf2ba38a8a94`. Прежние сведения о production 0.6 и неразвернутом кандидате больше не описывают текущую установку.

Точный установленный SHA: `bb98d3e42534e59583237fb07237cf2ba38a8a94`. Все три контейнера повторно проверены healthy, restart 0 / OOM false. Схема 2 и приватные mounts сохранены. [GitHub 39737711667](https://github.com/nd4y/phonoteka/actions/runs/39737711667) проверил 610 backend / 170 frontend tests и 19 ASR / 7 combined / 15 ordinary критериев; postflight проверил 42 target / 19 origin checks и 63 Grafana queries. Подробности - CHECK-036..038 [реестра](requirements-register.md).

**Полное ТЗ и Production Ready пока не подтверждены.** [Ревью готовности](production-readiness.md) отдельно перечисляет найденные дефекты, исправления кандидата, точные проверки и открытые условия выпуска. Наличие кода, работающего контейнера или технического succeeded не подменяет приемку продукта и качество музыки.

| Область | Реализовано и проверено | Открытая граница |
| --- | --- | --- |
| Безопасность | Single owner, Argon2, server sessions/idle/absolute expiry, CSRF/origin, scoped revocable tokens, эффектные операции с authorization | Финальный browser logout/cache/session protocol; новое security patch dependencies еще требует gate/поставки |
| Каталог/форматы | Content signatures, Unicode, SHA/revisions, native MP3/FLAC/MP4/Vorbis/Opus/WAV/AIFF, readonly/invalid profiles, CUE и PCM-verified derivatives | Поврежденные и неизвестные файлы остаются с причинами; нет обещания восстановить произвольное повреждение |
| Очереди | Durable bounded async/thread workers, independent inference, pause/cancel/resume/restart/idempotency, CAS, storage/provider waits | Исправления live reconnect и устаревших UI-ответов проходят отдельный выпуск ревью |
| Провайдеры | MusicBrainz/LRCLIB/CAA/local/ShazamIO, согласия/права/TTL/cache/budgets/retry, YouTube provenance и owner confirmation | No match и поисковый кандидат не доказывают идентичность; keyed providers выключены без настройки |
| ASR/ALIGN | Отдельный pinned CPU process/container, лимиты памяти/времени/PCM/result, durable jobs, независимый текст и DTW | Музыкальная приемка не пройдена: последний разрешенный пилот 0/66 строк; только черновики, автоматический quality gate выключен |
| Запись/откат | Проверенные backups, packet identity, native reopen/full decode, journals/atomic replace/CAS; реальные ID3v1/once дефекты исправлены | Каждая запись требует подходящего профиля/прав/проверок, неисправное аудио не пропускается через ослабленный guard |
| Ready/доставка | Version-bound review, план/hash/CAS, manifests/M3U8/sidecars/leases/receipts, восстановление и recovery gate | Телефонный offline Poweramp, подтверждение слов/таймкодов и итогового пакета отложены владельцем |
| UI/PWA/player | Плеер с очередью и CUE, Back, контекст после reload, public-only service worker, анимации/reduced motion, адаптивная верстка | Полный финальный browser/mobile/device protocol остается открытым; текущий этап выполняется без браузера |
| Эксплуатация | Matched GitHub images, controlled schema2 migration/checkpoint/deploy, NAS isolation, metrics/stdout/Grafana; bounded processing journal | Журнал P1 - частичный. OTel SDK/OTLP/Collector не развернуты и не считаются реализованными |

Снимок коллекции: 2142 аудио + 11 CUE, 525 файлов со встроенным LRC, 700 с обложкой, 1971 запись с отсутствующими данными/ошибками. Отчет владельца и независимая проверка качества отделены от технической готовности приложения.

Облачные источники и внешние ASR-адаптеры спроектированы, не реализованы. Новые функции без отдельного согласования не добавляются. Исторические стадии и неуспешные проверки сохранены в acceptance-audit/requirements-register и приватных протоколах, а не выдаются за текущее состояние.
