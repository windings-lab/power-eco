# power-graph

Live-графік споживання ПК і моніторів у браузері.

```
python3 server.py        # → http://localhost:8777/
```

## Звідки дані

| Що | Джерело | Точність |
|---|---|---|
| CPU | RAPL `intel-rapl:0/energy_uj` (package) | виміряно |
| GPU | `nvidia-smi` power.draw | виміряно |
| Решта (плата, RAM, SSD, вентилятори) | `rest_idle_w`..`rest_load_w` за навантаженням CPU+GPU | оцінка |
| Втрати БЖ | `/ ККД` за кривою `psu_curve` (Chieftec BDF-750C, Bronze) | оцінка |
| Монітори | стан із DRM + DDC/CI, ватти з `config.json` | оцінка |

### Стан моніторів
- `disconnected` у `/sys/class/drm/*/status` → **вимкнено** (0 W). DP-монітор, вимкнений кнопкою, зазвичай зникає саме так.
- підключений, але вихід вимкнено в KDE або DPMS Off → **очікування**.
- монітори з DDC/CI (HP E243, Dell 2007FP; `"ddc": true` у config): кожні 10 с опитується VCP D6 (режим живлення) і VCP 10 (яскравість); ватти масштабуються з яскравістю.
  D6 ≠ 01 або мовчання DDC → **вимкнено кнопкою**. Це важливо для Dell: він за перехідником DP→DVI, тому роз'єм завжди «connected».
- Sony TV не підтримує DDC: якщо вимкнути його пультом, а роз'єм лишиться «connected», визначити це неможливо.

## Режим економії (power-eco)

```
systemctl start power-eco   # CPU ≤60% частоти, EPP=power, ліміт 65 W; GPU ≤1000/810 MHz, 100 W
systemctl stop power-eco    # повернути налаштування, що були до старту
```

Сервіс не вмикається при завантаженні. Ліміти задаються в `power-eco`. Після зміни їх треба встановити заново:

```
sudo install -m 755 power-eco /usr/local/bin/power-eco
sudo install -m 644 power-eco.service /etc/systemd/system/ && sudo systemctl daemon-reload
```

## Доступ до CPU (RAPL)

`energy_uj` за замовчуванням читає лише root (захист від атаки через побічний канал PLATYPUS). Дати доступ групі `wheel`:

```
echo 'z /sys/class/powercap/intel-rapl:0/energy_uj 0440 root wheel -' | sudo tee /etc/tmpfiles.d/rapl-read.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/rapl-read.conf
```

Правило застосовується знову на кожному завантаженні. Без нього графік працює, але CPU показується як «немає доступу».

## Windows / WSL

Під WSL `server.py` сам вмикає режим **windows** (`--windows` / `--linux` або `"mode"` у config.json). Запуск: `uv run --no-project python server.py`.

| Що | Джерело у Windows |
|---|---|
| CPU | LibreHardwareMonitor `CPU Package` (`/data.json`) → лічильник Energy Meter → оцінка за завантаженням (`windows.cpu_idle_w`..`cpu_max_w`) |
| GPU | `nvidia-smi` |
| Монітори | імена/активність з WMI, DDC/CI (VCP D6/10) через dxva2 — та сама логіка, що й на Linux |

Справжні ватти CPU: `scoop install extras/librehardwaremonitor`, запустити **від адміністратора**, Options → Remote Web Server → Run (порт 8085). Якщо не запущено — працює оцінка.

### power-eco у Windows

```
power-eco start|stop|status|rapl     # у WSL: викликає power-eco.ps1 на боці Windows
```

Потрібен адмін (UAC-запит). Стан зберігається в `%LOCALAPPDATA%\power-eco\state.json`.

- **CPU:** ліміт RAPL PL1 = PL2 = 25 W (інше значення: `power-eco start 45`) пишеться в `MSR_PKG_POWER_LIMIT` (0x610) через драйвер PawnIO (ставиться разом із LibreHardwareMonitor) і модуль `IntelMSR.bin` з [PawnIO.Modules 0.2.11](https://github.com/namazso/PawnIO.Modules/releases) (скрипт сам завантажує його й перевіряє SHA256). `stop` повертає початкове значення регістра; перезавантаження теж скидає ліміт. `power-eco rapl` показує поточні PL1/PL2.
- `powercfg` (макс. стан 60%, EPP=power, boost вимкнено) теж виставляється, але під Hyper-V/WSL2 Windows ці обмеження ігнорує.
- **GPU:** `nvidia-smi.exe` -lgc/-lmc/-pl.
Для systemd-сервісу в WSL встановіть обидва файли: `sudo install -m 755 power-eco /usr/local/bin/ && sudo install -m 644 power-eco.ps1 /usr/local/bin/`.
