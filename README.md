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
| Решта (плата, RAM, SSD, вентилятори) | `rest_of_system_w` | оцінка |
| Втрати БЖ | `/ psu_efficiency` | оцінка |
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
