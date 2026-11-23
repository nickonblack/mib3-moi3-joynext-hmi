# MIB3 MOI3 JOYNEXT HMI Lab

Стенд для запуска штатного интерфейса Volkswagen MIB3 MOI3 JOYNEXT на Mac с Apple Silicon. Оригинальные JVM, Java-приложение и JNI работают в изолированном Linux ARM64-контейнере. Тестовые сервисы DSI/RSI дают интерфейсу начальное состояние, а окно macOS отображает кадры и передаёт нажатия мыши.

## Поддерживаемая прошивка

| Платформа | Версия | Вариант HMI | Разрешение |
| --- | --- | --- | --- |
| Volkswagen MIB3 MOI3 JOYNEXT | SW0334, `MOI3_EU_VWMQB_P33400P` | `OILarge2_EU` (`high`) | 1280×720 |

Нужен собственный ZIP `MOI3_SEQREL_EU_VWMQB_P33409P_3G8919360EP.zip` с вложенным train `P33400P`. Подготовка сверяет SHA-256 каждого из 111 выбранных пакетов и собирает локальный rootfs. Архив, rootfs, образы и журналы сеансов хранятся локально.

## 1. Подготовка macOS

Установите и запустите Docker Desktop. Для сборки и запуска нужны Python 3.9+, Git, Xcode Command Line Tools со Swift и три процессора, доступных Docker VM. Команды выполняются из корня репозитория.

Для графики и окна используется закреплённая версия исходников lipedesign:

```sh
upstream=references/lipedesign-mib3-moi3-altscreen-455658b
mkdir -p references
git clone --filter=blob:none --no-checkout https://github.com/lipedesign/mib3-moi3-altscreen.git "$upstream"
git -C "$upstream" checkout --detach 455658b005d1544ab1f6d6e5c42f529c0381235f
```

## 2. Сборка

```sh
python3 scripts/prepare_joynext.py --archive "$HOME/Downloads/MOI3_SEQREL_EU_VWMQB_P33409P_3G8919360EP.zip"
docker build --platform linux/arm64 -t joynext-lg-tools:mu0883 -f lg/Dockerfile.tools lg
python3 scripts/build_joynext.py
python3 scripts/build_joynext_graphics.py
python3 scripts/build_joynext_viewer.py
```

`joynext-lg-tools:mu0883` содержит общие инструменты Debian. Команды сборки сохраняют идентификаторы образов и контрольные суммы в `runtime/joynext/`.

## 3. Запуск и проверка

```sh
python3 scripts/run_joynext.py --seconds 300 --graphics-proxy --rsi-mock --viewer
```

После появления Welcome нажмите **Cancel** в диалоге основного пользователя, затем **Меню** на рамке. Для завершения выберите **Эмулятор → Остановить эмулятор** или дождитесь окончания сеанса. Само закрытие окна оставляет сеанс работать до этого лимита.

После завершения проверьте зафиксированные данные сеанса:

```sh
python3 scripts/verify_joynext.py
```

Проверка читает журналы и сведения о контейнерах, сверяет контрольные суммы оригинальных ELF, JAR и сценария запуска. Сценарий `scripts/check_joynext_touch.py` принимает каталог отчёта активной сессии и повторяет проверку всех семи плиток через окно macOS. Модульные тесты подготовки и mock RSI выполняются отдельно:

```sh
python3 -B -m unittest discover -s tests
```

## Что проверено

- Штатное меню 1280×720, переход Welcome → REGULAR, открытие всех семи плиток и возврат в меню. Контейнер использует оригинальные JVM и прикладную логику интерфейса, а данные автомобиля и профиль пользователя синтетические.
- Графика передаётся в Mesa llvmpipe. Контейнер HMI запускается под UID 1003 с read-only rootfs, без внешней сети, устройств и дополнительных capabilities. Настройки сохраняются в памяти до конца сеанса.
- Радио и медиа могут показывать штатный экран недоступности сервиса. Звук, Bluetooth, CarPlay, навигация и приборная панель за пределами стенда. Граница эмуляции проходит по пользовательскому пространству Linux.

Работа тестовых DSI/RSI описана в [протоколе стенда](joynext/PROTOCOL.md).

## Происхождение

Ориентир для проекта — стенд MIB2 High, запускающий штатный интерфейс Harman MHI2 на QEMU. Оттуда взят подход к документации: указать проверенный профиль прошивки, дать отдельные шаги подготовки и запуска, перечислить проверенные функции. [iscle/qemu-mhi2](https://github.com/iscle/qemu-mhi2)

Графический мост и Swift-просмотрщик взяты из проекта lipedesign под MIT. Здесь хранится патч русской панели, а лицензия оригинала копируется в собранное приложение. Mock-сервисы DSI/RSI написаны для JOYNEXT. [lipedesign/mib3-moi3-altscreen](https://github.com/lipedesign/mib3-moi3-altscreen/tree/455658b005d1544ab1f6d6e5c42f529c0381235f) · [Лицензия MIT](joynext/LICENSE.lipedesign)
