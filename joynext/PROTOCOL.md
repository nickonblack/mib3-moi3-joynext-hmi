# Наблюдаемый JOYNEXT TSD/DSI/RSI контракт

Источник: неизменённые P33400P `libtsd.dsi.so`, `tsd.dsi.jar`, HMI H40.272.318,
статический bytecode/disassembly и реальный `commgr.jsonl`. Это частичная модель
сервисов для одной HMI; адреса peers и их топология синтетические.

## TSD socket и discovery

Abstract AF_UNIX `@tsd.communication.commgr`. Все числа little-endian.
Header28: payloadLength:uint32, event:uint32, sender:uint64, destination:uint64,
type:uint8, reserved3. Negotiation type5/payload24: version1:uint32, bits8:uint8,
padding3, clientBase:uint64, NameServer:uint64. Client base = id << 56,
NameServer = 0x1000001, synthetic DSI peer = 0x1000010.

NameServer event2→3 register и6→7 query. Tags: string37 + NUL, uint64 33 +8bytes,
bool true1/false2. HMI регистрирует HMITransceiver и HMITransceiverInv; наш mock
возвращает domain `/tux`. После второй регистрации и detach временного watcher:

1. Peer connect type1 → subscriptions type6 для ADM/hello/pong/PCC/services.
2. Первое direct discovery событие инициализирует JNI и там поглощается.
3. Получаем native PCC-ready event0x20fe000 от HMI.
4. Отправляем по одному событию0x20ff800 с base event каждого service instance0.
5. Original JNI/Java самостоятельно создают/register service instances и подписки.

DSI wire payload: tag42, uint32 length, затем вложенный RpcBuffer. Три первых int
в body: event, versionHash, method. Int tag13 +4bytes, long key tag34 +8bytes;
nullable present39/null38, string37+NUL, array4+uint32count+tagged elements.
Byte элементы имеют tag9 и sign-extended int32. Replies: base event | 0x800.
Запросы HMI идут broadcast type3; ответы mock — direct type0 в HMITransceiver.

| Service instance0 | Base event | Version hash |
|---|---:|---:|
| DSIPersistence | 33796096 / 0x203b000 | 1504078919 |
| DSIPowerManagement | 33816576 | -618193930 |
| DSICarTimeUnitsLanguage | 33632256 | -618193897 |
| DSIGeneralVehicleStates | 33660928 | -618193901 |
| DSIKeyPanel | 33673216 | -618193839 |
| DSIDisplayManagement | 33652736 | -618193872 |

Method0 = setNotification(int[]), 3839 = notifyForAttribute(int). HMI фактически
подписывается и получает initial state. Unsupported methods/attributes логируются,
не маскируются универсальным success. Нет arbitrary routing/deregistration/redirects,
CommMgr heartbeat и R7/CARCOM не воспроизведены.

## DSIPersistence

Attribute1: synthetic storage medium1 + valid1. Хранилище на самом деле RAM-only.
Запрос: namespace:int, key:long, затем typed value для записи. Для read-with-timeout
ещё timeout:int. Методы1000–1009: alternating write/read int, buffer, string, int[],
string[]. Read-with-timeout1019–1023 преобразуются к соответствующему read.
Replies2000–2009 содержат namespace/key, значение для чтения и status:int.

- Отсутствующий ключ/type mismatch: status2 INVALID_KEY и типизированное пустое значение.
  Original persistence layer использует default settings; profileId1 — штатный fallback.
- Запись: сохранить проверенный wire value, status0. Следующее чтение того же типа
  возвращает сохранённые байты. Лимит256 ключей, около64KiB wire value; переполнение
  возвращает status3. Значения исчезают при остановке контейнера.
- Проверены на original HMI readBuffer1003/reply2003 и writeBuffer1002/reply2002;
  другие типы реализованы общей передачей typed value, отдельного original-runtime
  покрытия каждого типа пока нет. SQL/erase/profile-management API не реализованы.
- Self-test проверяет missing/read/write/read непустого byte buffer, короткие/некорректные
  payloads. Это дополняет реальную трассу, не заменяет её.

## Ввод, display и startup состояния

В `/run/display/dsi-input` viewer через upstream ponte добавляет строки:

```text
tecla 13 78 1
tecla 13 78 0
gesto 4 360 175
gesto 1 360 175 1
encoder 13 17 1
```

Между press/release выдерживать около100ms. Menu = key78, touchscreen keyboard13
сверены с original `org.dsi.ifc.keypanel.Constants`. Key2 attribute25 и Encoder2
attribute23: keyboard, key, state/increment, monotonic timestamp ms, valid1.
Gesture2 attribute20: keyboard13, gesture, fingers, palm=false, x, y, p1, p2,
timestamp, valid1. Жест4 press,1 tap. Проверены Cancel, семь плиток главного меню,
возврат Menu и кнопка Settings; encoder и остальные клавиши не проверены визуально. Ограничены размер строки, числа и координаты1280×720.
Длинная/некорректная строка не отправляется в native клиент.

### Исправление координат — 13 ноября 2026

Original runHMI.sh задаёт `de.vw.mib.asl.keypanel.touch.scaleX=2` и `scaleY=2`:
физический ABT передаёт половинные координаты. Swift viewer переводит mouse point
в полные пиксели текущего кадра, с учётом размера окна и обратного направления Y.
Поэтому прежнее нажатие Settings(1060,170) попадало в(2120,340), за пределы1280×720.
Cancel случайно работал при таком масштабе из-за расположения элементов диалога.

Диагностика: baseline `reports/joynext-hmi-20261112T223553493462Z` не открывал
Settings по полным координатам; (530,85) открыл SYSTEM_SETUP_MAIN. Original
TouchInputDeviceImpl$1 умножает x/y на эти properties; байткод сохранён в
`reports/joynext-touch-java.txt`. Приоритет `_JAVA_OPTIONS` над scripted `-D...=2`
проверен самой original Zulu: `reports/joynext-touch-property-priority.txt`.

В `probe.sh` только для lab profile заданы обе scale properties=1. Координаты mock
остаются полными пикселями; original script/classes не патчатся, точность не теряется.
Нет привязки «координата плитки → клавиша Settings»: все нажатия идут через native
viewer mouseDown/mouseUp → Gesture2 → оригинальные Widgets/hit testing.

`check_joynext_touch.py REPORT` запускает русское окно с native NSEvent-сценарием,
сохраняет7 исходных HMI PNG и снимок панели. Проверяет последовательность Menu →
SYSTEMSETUP/RADIO/CAR/USERMANAGEMENT/MEDIA/TONE/QUICKSTARTGUIDE, используя original
routing log. Backend availability и полный multitouch этим тестом не проверяются.

Русская панель строится `build_joynext_viewer.py` из неизменённого MIT snapshot455658b
+ `joynext/viewer-ru.patch`; upstream SHA и скомпилированный executable записаны в
`runtime/joynext/viewer/viewer-lock.json`. В app своя bundle identity. Команда остановки
создаёт shared tmpfs `/run/display/stop`, launcher собирает maps/SHA/logs и завершает
сессию. Старый LG launcher не вызывается. Место original source и его LICENSE сохранены.

Display: размеры1280×720, extents, power state, brightness/contrast/colour/tint,
showDisplayables/deferDisplayables. Поддержаны ID0–255 (HMI использует43).
KeyPanel setGenericSetting получает echo reply2000; getProperty пока отсутствует.
Power сообщает ON, vehicle standstill/parking brake=true, остальные поддержанные
boolean states=false; time/language даёт en_GB и минимальные units. Это синтетические
состояния, не реконструкция реального автомобиля и не полный контракт этих служб.

## RSI

Python standard library HTTP/VIWI WS mock слушает127.0.0.1:443 и7770 в namespace
network-none HMI. Registry `/api/v1/`, services usermanagement0.6.0 и
onlineregistration1.1.3; users, userContexts, processes(empty), primaryUsers.
Queries/field filters не применяются: fixture содержит единственного lab user/context.

WS subscribe ACK и initial data обязаны содержать `status:"ok"`, code200 и тот же
event включая query/fragment; без status original RSI считает ACK ошибкой.
Клиентские frames masked/unfragmented, max64KiB. Ping/pong/unsubscribe/close поддержаны.
HTTP POST разрешён только для boolean isHMIReady существующего userContext, без
изменения identity. Повтор неизменного значения не вызывает уведомления; изменения
рассылаются только подписчикам этого context/collection.

Lab Guest = synthetic level2 secondaryUser, securityMode=false, tokenStatus=ok.
Никаких реальных credentials/token нет. LastSync — original sentinel
`1970-01-01T00:00:00.000+00:00`; формат сверён с UserManagementAPIListenerAdapter.
PrimaryUsers содержит элемент с `nickname:""`: это отсутствие primary user, которое
original Welcome controller отличает от ещё не пришедшего primaryUsers-ответа.
ffbKeyAllocation=off. Только после этих данных Welcome переходит в REGULAR.

## Воспроизведение и происхождение

Сборка C/self-test: `python3 scripts/build_joynext.py`.
Сборка GL shim с оригинальной JOYNEXT libc: `python3 scripts/build_joynext_graphics.py`.
Запуск/verification: команды README. Данные оригинальной прошивки не публиковать.
Каждый report содержит command/isolation, harness SHA, HMI/IPC/RSI logs, maps/status,
original SHA, GL lock/frame. `verify_joynext.py` требует реального original read/reply,
startup-profile ready, SHA/изоляцию, а с графикой — nonblank1280×720+Mesa.
При RSI требует успешный POST и REGULAR. Verifier не делает OCR и не утверждает,
что любой nonblank frame является меню; выбранные PNG проверены отдельно визуально.

GL forwarding и Swift viewer: lipedesign, snapshot455658b, MIT, LICENSE сохранён
в references. Код forwarding не изменён, пересобран против JOYNEXT libc. Идеи RSI
fixtures сверены с upstream stubs и оригинальным JOYNEXT bytecode; реализация TSD/RSI
наша. LG KIPC/protobuf и LG firmware binaries в этом запуске не используются.
