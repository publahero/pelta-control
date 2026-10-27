import os, sys, re, shutil, subprocess, math, struct, time, select, json, hashlib, threading, urllib.request, urllib.error, tempfile
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QSize, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QFont, QAction
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
 QLabel, QPushButton, QSlider, QSystemTrayIcon, QMenu, QCheckBox, QTabWidget, QProgressBar, QComboBox, QLineEdit, QMessageBox)

APP_ID='io.github.peltacontrol.PeltaControl'

def run(*args):
    try:
        p=subprocess.run(args, capture_output=True, text=True, timeout=5)
        return p.stdout.strip() if p.returncode==0 else ''
    except (OSError, subprocess.TimeoutExpired): return ''

def devices():
    # wpctl status has separate Sinks and Sources; restrict to actual output sinks.
    out=run('wpctl','status')
    section=re.search(r'(?im)^\s*[│ ]*├?─?\s*Sinks:\s*$([\s\S]*?)(?=^\s*[│ ]*[├└]─?\s*(?:Sources|Filters|Streams|Video|Settings):|\Z)',out,re.M)
    block=section.group(1) if section else out
    # Allow stars, tree glyphs and ALSA suffix without treating the source as a sink.
    for line in block.splitlines():
        if 'pelta' not in line.lower(): continue
        m=re.search(r'(?<!\d)(\d+)\.\s+(.*)',line)
        if m:return int(m.group(1)),m.group(2).strip()
    return None,None

def mic_devices():
    raw=run('wpctl','status')
    # Scan exclusively in Sources, excluding Streams and Monitors.
    match=re.search(r'(?im)^.*?Sources:\s*$([\s\S]*?)(?=^.*?(?:Filters|Streams|Video|Settings):\s*$|\Z)',raw,re.M)
    if not match:return []
    result=[]
    for line in match.group(1).splitlines():
        m=re.search(r'(?<!\d)(\d+)\.\s+(.+)',line)
        if m and 'monitor' not in m.group(2).lower():
            result.append((int(m.group(1)),m.group(2).strip()))
    return result

def mic_state(source):
    text=run('wpctl','get-volume',str(source)) if source is not None else ''
    m=re.search(r'Volume:\s*([0-9.]+)',text)
    return ((min(150,round(float(m.group(1))*100)), '[MUTED]' in text) if m else (None,False))

class MicrophoneMeter:
    """Read a PipeWire-Pulse source via parec; never save microphone audio."""
    def __init__(self):self.proc=None;self.source=None;self.buffer=bytearray();self.peak=-90.0;self.peak_time=0
    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:self.proc.wait(timeout=0.4)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait()
        self.proc=None;self.buffer.clear();self.source=None
    def start(self,source):
        if source==self.source and self.proc and self.proc.poll() is None:return True
        self.stop()
        if source is None or not shutil.which('parec'):return False
        try:
            # PipeWire's PulseAudio-compatible server resolves numeric source IDs
            # differently from wpctl IDs, so use the Source name from wpctl inspect.
            props=run('wpctl','inspect',str(source))
            name=re.search(r'node\.name\s*=\s*"([^"]+)"',props)
            target=name.group(1) if name else None
            if not target:return False
            self.proc=subprocess.Popen(['parec','--device='+target,'--format=float32le','--rate=48000','--channels=1','--raw'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)
            os.set_blocking(self.proc.stdout.fileno(),False)
            self.source=source
            return True
        except (OSError,ValueError):self.stop();return False
    def poll(self):
        if not self.proc or self.proc.poll() is not None:return None
        try:
            for _ in range(8):
                b=os.read(self.proc.stdout.fileno(),16384)
                if not b:break
                self.buffer.extend(b)
        except BlockingIOError:pass
        except OSError:return None
        count=(len(self.buffer)//4)*4
        if count==0:return None
        chunk=self.buffer[:count];del self.buffer[:count]
        # Reduce memory and CPU even if a large backlog has accumulated.
        samples=memoryview(chunk).cast('f')
        if not samples:return None
        power=sum(x*x for x in samples if math.isfinite(x))/len(samples)
        rms=20*math.log10(max(1e-8,math.sqrt(power)))
        peak=max((abs(x) for x in samples if math.isfinite(x)),default=0.0)
        now=time.monotonic();instant=20*math.log10(max(1e-8,peak))
        if instant>=self.peak or now-self.peak_time>3:
            self.peak=instant;self.peak_time=now
        return max(-90,min(0,rms)),max(-90,min(0,self.peak))

def volume(sink):
    raw=run('wpctl','get-volume',str(sink)) if sink is not None else ''
    m=re.search(r'Volume:\s*([0-9.]+)',raw)
    return round(float(m.group(1))*100) if m else None

def battery():
    # UPower devices sometimes expose Bluetooth headset battery through BlueZ.
    for path in run('upower','-e').splitlines():
        if 'pelta' in path.lower() or 'headset' in path.lower():
            info=run('upower','-i',path)
            match=re.search(r'percentage:\s*(\d+)%',info)
            if match: return int(match.group(1)), 'UPower'
    # BlueZ D-Bus battery exposed by bluetoothctl info; only accept a Pelta-branded device.
    listed=run('bluetoothctl','devices')
    for line in listed.splitlines():
        m=re.search(r'^Device\s+([0-9A-F:]{17})\s+(.*Pelta.*)$',line,re.I)
        if m:
            info=run('bluetoothctl','info',m.group(1))
            if 'Connected: yes' not in info: continue
            level=re.search(r'Battery Percentage:\s*(?:0x[0-9a-f]+\s*\()?([0-9]+)\)?',info,re.I)
            if level:
                n=int(level.group(1))
                if 0<=n<=100: return n, 'Bluetooth'
    return None,None

def pelta_cli():
    # Bundled upstream backend first, never another random pelta-control in PATH.
    location=Path(sys.executable).resolve().parent.parent/'PeltaBackend'/'PeltaBackend'
    if location.is_file():return str(location)
    # Developer / host-installed fallback.
    return shutil.which('pelta-control')

def hid_battery():
    cmd=pelta_cli()
    if not cmd:return None,None
    try:
        p=subprocess.run([cmd,'battery'],capture_output=True,text=True,timeout=6)
        if p.returncode != 0:return None,None
        m=re.search(r'(?<!\d)(100|[1-9]?\d)\s*%',p.stdout)
        if m:return int(m.group(1)),'USB HID'
        # CLI might print numeric-only percentage.
        if re.fullmatch(r'\s*(100|[1-9]?\d)\s*',p.stdout):return int(p.stdout.strip()),'USB HID'
    except (OSError,subprocess.TimeoutExpired):pass
    return None,None

def tray_icon(level):
    # Draw at high resolution: KDE scales this image down to its panel icon size.
    # The numerals deliberately occupy almost the full width of the icon.
    img = QPixmap(192, 192)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    ink = QColor('#f8fafc')
    if level is not None and level <= 20:
        ink = QColor('#fb7185')
    painter.setPen(QPen(ink, 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    # A wide, simple battery at the top; avoids competing with the percentage.
    painter.drawRoundedRect(38, 13, 105, 59, 10, 10)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(ink)
    painter.drawRoundedRect(148, 32, 10, 20, 3, 3)
    if level is not None:
        filled = max(0, min(100, level))
        painter.drawRoundedRect(48, 23, round(85 * filled / 100), 39, 4, 4)
    painter.setPen(ink)
    font = QFont('DejaVu Sans')
    font.setWeight(QFont.Weight.Black)
    font.setPixelSize(69 if level is not None and level < 100 else 62)
    painter.setFont(font)
    # Wide text area accommodates 100% and remains readable downscaled.
    text = f'{level}%' if level is not None else '--'
    painter.drawText(0, 83, 192, 92, Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return QIcon(img)

# Public GitHub release updater. Downloads a new version to an owned path.
VERSION = '0.5.2'
CONFIG_DIR = Path.home()/'.config/pelta-control'
APP_HOME = Path.home()/'.local/share/pelta-control'
TARGET = APP_HOME/'Pelta-Control.AppImage'
LAUNCHER = Path.home()/'.local/bin/pelta-control'
DESKTOP = Path.home()/'.local/share/applications/pelta-control.desktop'
OLD_DESKTOP = Path.home()/'.local/share/applications/pelta-control-updates.desktop'
SINGLE_INSTANCE_NAME = 'io.github.peltacontrol.PeltaControl-' + str(os.getuid())

class UpdateSignals(QObject):
    done = Signal(object)

def github_json(repo):
    req = urllib.request.Request('https://api.github.com/repos/'+repo+'/releases/latest', headers={
        'User-Agent':'Pelta-Control/'+VERSION, 'Accept':'application/vnd.github+json'})
    with urllib.request.urlopen(req,timeout=12) as r:
        return json.load(r)

def valid_repo(s):
    return re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',s or '') is not None

def version_parts(value):
    # Compare 0.5 and 0.5.0 as equivalent versions.
    segments=[int(x) for x in value.split('.')]
    return tuple((segments+[0,0,0])[:3])

def asset_url(release, name):
    for a in release.get('assets', []):
        if a.get('name') == name and a.get('browser_download_url','').startswith('https://'):
            return a['browser_download_url']
    return None

def fetch_bytes(url,limit):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent':'Pelta-Control/'+VERSION}),timeout=45) as resp:
        data=resp.read(limit+1)
    if len(data)>limit:raise ValueError('Ladattava tiedosto ylittää kokorajan')
    return data

def prepare_update(repo,version, release):
    name=f'Pelta-Control-{version}-x86_64.AppImage'
    url=asset_url(release,name)
    if not url:raise RuntimeError('Julkaisusta puuttuu AppImage: '+name)
    checksum_url=asset_url(release,name+'.sha256')
    manifest_url=asset_url(release,'SHA256SUMS')
    if checksum_url:
        expected=fetch_bytes(checksum_url,4096).decode('ascii').strip().split()[0].lower()
    elif manifest_url:
        manifest=fetch_bytes(manifest_url,65536).decode('utf-8')
        matches=[]
        for line in manifest.splitlines():
            parts=line.strip().split(maxsplit=1)
            if len(parts)!=2:continue
            digest,filename=parts
            if filename.lstrip('*').strip()==name:matches.append(digest.lower())
        if len(matches)!=1:raise ValueError('SHA256SUMS ei sisällä yksiselitteistä AppImagen tarkistussummaa')
        expected=matches[0]
    else:
        raise RuntimeError('Julkaisusta puuttuu AppImagen .sha256 tai SHA256SUMS')
    if not re.fullmatch(r'[0-9a-f]{64}',expected):raise ValueError('Tarkistussummatiedosto ei kelpaa')
    data=fetch_bytes(url,400*1024*1024)
    if hashlib.sha256(data).hexdigest()!=expected:raise ValueError('SHA256-tarkistus epäonnistui')
    if not data.startswith(b'\x7fELF'):raise ValueError('Ladattu tiedosto ei ole AppImage/ELF')
    APP_HOME.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=APP_HOME, prefix='.update-',delete=False) as f:
        path=Path(f.name);f.write(data);f.flush();os.fsync(f.fileno())
    path.chmod(0o755)
    return path

def install_managed_copy(src):
    APP_HOME.mkdir(parents=True,exist_ok=True)
    LAUNCHER.parent.mkdir(parents=True,exist_ok=True)
    DESKTOP.parent.mkdir(parents=True,exist_ok=True)
    tmp=APP_HOME/'.installing.AppImage'
    shutil.copyfile(src,tmp)
    tmp.chmod(0o755)
    os.replace(tmp,TARGET)
    LAUNCHER.write_text('#!/bin/sh\nexec "'+str(TARGET)+'" "$@"\n')
    LAUNCHER.chmod(0o755)
    DESKTOP.write_text('[Desktop Entry]\nType=Application\nName=Pelta Control\nComment=ASUS ROG Pelta Wireless Headset Manager\nExec='+str(LAUNCHER)+'\nIcon=audio-headphones\nCategories=Utility;\nTerminal=false\nStartupNotify=true\n')
    OLD_DESKTOP.unlink(missing_ok=True)

class Window(QMainWindow):
    def __init__(self, app):
        super().__init__();self.app=app
        self.setWindowTitle('Pelta Control 0.5.2');self.resize(430,360)
        self.setStyleSheet('QWidget{background:#121826;color:#f1f5f9;font-size:14px} QPushButton{background:#26354a;border:1px solid #3b506b;border-radius:9px;padding:10px} QPushButton:hover{background:#354762} QSlider::groove:horizontal{height:6px;background:#39475a;border-radius:3px} QSlider::handle:horizontal{background:#50b7fb;width:16px;margin:-6px 0;border-radius:8px}')
        self.meter=MicrophoneMeter();self.source=None;self.mic_poll=QTimer(self);self.mic_poll.timeout.connect(self.update_meter)
        tabs=QTabWidget();self.setCentralWidget(tabs);self.resize(510,475)
        root=QWidget();tabs.addTab(root,'Kuulokkeet');lay=QVBoxLayout(root);lay.setSpacing(17);lay.setContentsMargins(25,23,25,23)
        heading=QLabel('🎧  ASUS ROG Pelta Wireless');heading.setStyleSheet('font-size:21px;font-weight:700');lay.addWidget(heading)
        self.link=QLabel('Etsitään äänilaitetta…');lay.addWidget(self.link)
        self.batt=QLabel('Akku: ei tietoa');self.batt.setStyleSheet('font-size:24px;font-weight:600');lay.addWidget(self.batt)
        self.voltext=QLabel('Äänenvoimakkuus: –');lay.addWidget(self.voltext)
        self.slider=QSlider(Qt.Orientation.Horizontal);self.slider.setRange(0,100);self.slider.setEnabled(False);self.slider.valueChanged.connect(self.set_volume);lay.addWidget(self.slider)
        btnrow=QHBoxLayout();self.off=QPushButton('LED pois');self.on=QPushButton('LED päälle');btnrow.addWidget(self.off);btnrow.addWidget(self.on);lay.addLayout(btnrow)
        self.off.clicked.connect(lambda:self.led('off'));self.on.clicked.connect(lambda:self.led('on'))
        self.status=QLabel('USB HID -ohjaus tarkistetaan…');self.status.setWordWrap(True);self.status.setStyleSheet('color:#9aaec5');lay.addWidget(self.status)
        self.autostart=QCheckBox('Käynnistä kirjautuessa');self.autostart.setChecked(self.has_autostart());self.autostart.toggled.connect(self.set_autostart);lay.addWidget(self.autostart)
        lay.addStretch()
        micro=QWidget();tabs.addTab(micro,'Mikrofoni');ml=QVBoxLayout(micro);ml.setSpacing(13);ml.setContentsMargins(22,18,22,20)
        ml.addWidget(QLabel('Mikrofonin äänilähde'))
        self.micselect=QComboBox();self.micselect.currentIndexChanged.connect(self.change_mic);ml.addWidget(self.micselect)
        self.mictext=QLabel('Mikrofonin voimakkuus: ei saatavilla');ml.addWidget(self.mictext)
        self.micslider=QSlider(Qt.Orientation.Horizontal);self.micslider.setRange(0,150);self.micslider.setEnabled(False);self.micslider.valueChanged.connect(self.set_mic_volume);ml.addWidget(self.micslider)
        self.mute=QPushButton('Mykistä mikrofoni');self.mute.clicked.connect(self.toggle_mute);ml.addWidget(self.mute)
        self.dbtext=QLabel('Taso: -- dBFS    Huippu: -- dBFS');self.dbtext.setStyleSheet('font-size:16px;font-weight:bold');ml.addWidget(self.dbtext)
        self.meterbar=QProgressBar();self.meterbar.setRange(0,90);self.meterbar.setValue(0);self.meterbar.setTextVisible(False);self.meterbar.setMinimumHeight(24);ml.addWidget(self.meterbar)
        self.micstatus=QLabel('Mittari käynnistyy, kun mikrofoni löytyy.');self.micstatus.setWordWrap(True);self.micstatus.setStyleSheet('color:#9aaec5');ml.addWidget(self.micstatus)
        ml.addStretch()
        eq=QWidget();tabs.addTab(eq,'EQ');el=QVBoxLayout(eq);el.setContentsMargins(25,25,25,25)
        title=QLabel('Äänen taajuuskorjain');title.setStyleSheet('font-size:19px;font-weight:bold');el.addWidget(title)
        msg=QLabel('Avaa EasyEffects kuulokkeiden ja mikrofonin erillisten taajuuskorjaimien säätämiseen. Tämä välilehti ei muuta EQ-asetuksia itsenäisesti.');msg.setWordWrap(True);el.addWidget(msg)
        eqbutton=QPushButton('Avaa EasyEffects');eqbutton.clicked.connect(self.open_eq);el.addWidget(eqbutton);el.addStretch()
        updates=QWidget();tabs.addTab(updates,'Päivitykset');ul=QVBoxLayout(updates)
        ul.addWidget(QLabel('GitHub Releases -päivitykset'))
        ul.addWidget(QLabel('Julkaisijan GitHub-tunnus ja repository (esim. kayttaja/pelta-control):'))
        self.repo_field=QLineEdit();self.repo_field.setPlaceholderText('publahero/pelta-control');ul.addWidget(self.repo_field)
        try:self.repo_field.setText(json.loads((CONFIG_DIR/'update.json').read_text()).get('repo',''))
        except (OSError,ValueError):pass
        if not self.repo_field.text().strip():self.repo_field.setText('publahero/pelta-control')
        self.update_status=QLabel('Versio 0.5.1 – määritä GitHub-repository.');self.update_status.setWordWrap(True);ul.addWidget(self.update_status)
        self.check_button=QPushButton('Tarkista päivitykset');self.check_button.clicked.connect(self.check_updates);ul.addWidget(self.check_button)
        self.update_button=QPushButton('Lataa ja ota päivitys käyttöön');self.update_button.setEnabled(False);self.update_button.clicked.connect(self.download_update);ul.addWidget(self.update_button)
        self.update_note=QLabel('Päivitys asentuu käyttäjäkohtaiseen ~/.local/share/pelta-control -kansioon ja tulee käyttöön seuraavalla käynnistyksellä. Julkaisu edellyttää vastaavaa .sha256-tiedostoa.');self.update_note.setWordWrap(True);ul.addWidget(self.update_note)
        ul.addStretch()
        self.update_signals=UpdateSignals();self.update_signals.done.connect(self.on_update_done)
        self.release=None;self.release_version=None;self.updating=False
        QTimer.singleShot(2000,self.check_updates_silently)
        self.tray=QSystemTrayIcon(self);self.tray.setIcon(tray_icon(None));self.tray.setToolTip('ROG Pelta – akku ei tiedossa')
        menu=QMenu();a=menu.addAction('Avaa Pelta Control');a.triggered.connect(self.show_normal)
        menu.addSeparator();x=menu.addAction('LED pois');x.triggered.connect(lambda:self.led('off'))
        y=menu.addAction('LED päälle');y.triggered.connect(lambda:self.led('on'))
        menu.addSeparator();q=menu.addAction('Lopeta');q.triggered.connect(self.quit)
        self.tray.setContextMenu(menu);self.tray.activated.connect(self.tray_click)
        self.tray.show();self.sink=None
        self.timer=QTimer(self);self.timer.timeout.connect(self.refresh);self.timer.start(30000);self.refresh();self.mic_poll.start(100)

    def update_worker(self,job,fn):
        if self.updating:return
        self.updating=True;self.check_button.setEnabled(False);self.update_button.setEnabled(False)
        def do():
            try:self.update_signals.done.emit((job,True,fn()))
            except Exception as e:self.update_signals.done.emit((job,False,str(e)))
        threading.Thread(target=do,daemon=True).start()
    def current_repo(self):
        repo=self.repo_field.text().strip()
        if not valid_repo(repo):raise ValueError('GitHub-repository muodossa käyttäjä/projekti')
        CONFIG_DIR.mkdir(parents=True,exist_ok=True)
        (CONFIG_DIR/'update.json').write_text(json.dumps({'repo':repo}))
        return repo
    def check_updates_silently(self):
        if valid_repo(self.repo_field.text().strip()):self.check_updates()
    def check_updates(self):
        try:repo=self.current_repo()
        except Exception as e:self.update_status.setText(str(e));return
        self.update_status.setText('Haetaan viimeisintä julkaisua…')
        self.update_worker('check',lambda:github_json(repo))
    def on_update_done(self,result):
        job,ok,data=result
        self.updating=False;self.check_button.setEnabled(True)
        if not ok:self.update_status.setText('Päivitys epäonnistui: '+str(data));return
        if job=='check':
            tag=data.get('tag_name','').lstrip('v')
            if not re.fullmatch(r'\d+\.\d+(?:\.\d+)?',tag):
                self.update_status.setText('Uusimman julkaisun versionumero ei kelpaa');return
            self.release=data;self.release_version=tag
            cur=version_parts(VERSION);new=version_parts(tag)
            if new <= cur:
                self.update_button.setEnabled(False)
                self.update_status.setText('Uusin julkaisu: '+tag+' – olet ajan tasalla.');return
            self.update_status.setText('Uusi versio '+tag+' saatavilla!')
            self.update_button.setEnabled(True)
            self.tray.showMessage('Pelta Control','Uusi versio '+tag+' saatavilla',QSystemTrayIcon.MessageIcon.Information,5000)
        elif job=='download':
            staged=data
            try:
                original=os.environ.get('APPIMAGE')
                if TARGET.exists():
                    backup=APP_HOME/'Pelta-Control.previous.AppImage'
                    shutil.copy2(TARGET,backup)
                elif original and Path(original).is_file():
                    install_managed_copy(original)
                    shutil.copy2(TARGET,APP_HOME/'Pelta-Control.previous.AppImage')
                else:
                    raise RuntimeError('Nykyisen AppImagen sijainti ei ole tiedossa. Käynnistä AppImage-tiedostosta.')
                install_managed_copy(staged)
                if self.autostart.isChecked():self.set_autostart(True)
                self.update_status.setText('Asennettu versio '+str(self.release_version)+'. Sulje ohjelma ja käynnistä valikosta ”Pelta Control”.')
            except Exception as e:self.update_status.setText('Asennus epäonnistui: '+str(e))
            finally:Path(staged).unlink(missing_ok=True)
    def download_update(self):
        if not self.release or not self.release_version:return
        try:repo=self.current_repo()
        except Exception as e:self.update_status.setText(str(e));return
        answer=QMessageBox.question(self,'Asenna päivitys','Ladataanko ja asennetaanko julkaisu '+self.release_version+'? Julkaise vain itse tarkistamiasi AppImage-tiedostoja.')
        if answer!=QMessageBox.StandardButton.Yes:return
        self.update_status.setText('Ladataan ja tarkistetaan SHA256…')
        self.update_worker('download',lambda:prepare_update(repo,self.release_version,self.release))

    def refresh(self):
        self.sink,name=devices()
        self.link.setText('Äänilaite: '+name if name else 'Pelta-äänilaitetta ei löydy PipeWirestä')
        v=volume(self.sink);self.slider.setEnabled(v is not None)
        if v is not None:
            self.slider.blockSignals(True);self.slider.setValue(min(v,100));self.slider.blockSignals(False)
            self.voltext.setText(f'Äänenvoimakkuus: {v}%')
        else:self.voltext.setText('Äänenvoimakkuus: ei saatavilla')
        b,source=hid_battery()
        if b is None:b,source=battery()
        self.batt.setText(f'Akku: {b}%' if b is not None else 'Akku: ei tietoa')
        self.tray.setIcon(tray_icon(b));self.tray.setToolTip(f'ROG Pelta – {b}% ({source})' if b is not None else 'ROG Pelta – akkutieto ei saatavilla')
        self.refresh_mics()
        can_led=bool(pelta_cli());self.off.setEnabled(can_led);self.on.setEnabled(can_led)
        if not can_led:self.status.setText('USB HID -taustakomponentti puuttuu. Rakenna AppImage build-appimage.sh-skriptillä.')

    def refresh_mics(self):
        sources=mic_devices();old=self.source
        self.micselect.blockSignals(True);self.micselect.clear()
        for ident,name in sources:self.micselect.addItem(name,ident)
        ids=[i for i,_ in sources]
        # Prefer Pelta, otherwise preserve existing device or use the system's first source.
        selected=old if old in ids else next((i for i,n in sources if 'pelta' in n.lower()), ids[0] if ids else None)
        if selected is not None:self.micselect.setCurrentIndex(ids.index(selected))
        self.micselect.blockSignals(False)
        self.setup_mic(selected)
    def change_mic(self,index):
        if index>=0:self.setup_mic(self.micselect.itemData(index))
    def setup_mic(self,source):
        self.source=source
        v,muted=mic_state(source)
        self.micslider.setEnabled(v is not None)
        self.mute.setEnabled(v is not None)
        if v is None:self.mictext.setText('Mikrofonin voimakkuus: ei saatavilla')
        else:
            self.micslider.blockSignals(True);self.micslider.setValue(v);self.micslider.blockSignals(False)
            self.mictext.setText(f'Mikrofonin voimakkuus: {v}%')
        self.mute.setText('Poista mykistys' if muted else 'Mykistä mikrofoni')
        if not self.meter.start(source):
            self.micstatus.setText('Mittari ei käytettävissä. Tarvitaan PipeWire-Pulse ja parec (libpulse).')
            self.meterbar.setValue(0)
        else:self.micstatus.setText('Mittari näyttää mikrofonin digitaalista signaalitasoa (dBFS). Ääntä ei tallenneta.')
    def set_mic_volume(self,value):
        if self.source is None:return
        p=subprocess.run(['wpctl','set-volume',str(self.source),f'{value/100:.2f}'],capture_output=True,text=True,timeout=5)
        self.mictext.setText(f'Mikrofonin voimakkuus: {value}%' if p.returncode==0 else 'Mikrofonin säätö epäonnistui: '+p.stderr[:100])
    def toggle_mute(self):
        if self.source is None:return
        p=subprocess.run(['wpctl','set-mute',str(self.source),'toggle'],capture_output=True,text=True,timeout=5)
        if p.returncode!=0:self.micstatus.setText('Mykistys epäonnistui: '+p.stderr[:100]);return
        _,muted=mic_state(self.source);self.mute.setText('Poista mykistys' if muted else 'Mykistä mikrofoni')
    def update_meter(self):
        data=self.meter.poll()
        if data is None:return
        level,peak=data
        self.meterbar.setValue(max(0,min(90,round(level+90))))
        self.dbtext.setText(f'Taso: {level:.1f} dBFS    Huippu: {peak:.1f} dBFS')
    def open_eq(self):
        executable=shutil.which('easyeffects')
        if not executable:self.micstatus.setText('EasyEffects puuttuu. Asenna: sudo pacman -S easyeffects');return
        try:subprocess.Popen([executable],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        except OSError as e:self.micstatus.setText(f'EasyEffects ei käynnisty: {e}')

    def set_volume(self,n):
        if self.sink is None:return
        result=subprocess.run(['wpctl','set-volume',str(self.sink),f'{n/100:.2f}'],capture_output=True,text=True,timeout=5)
        if result.returncode != 0:
            self.voltext.setText('Äänenvoimakkuuden säätö epäonnistui: '+result.stderr[:90]);return
        self.voltext.setText(f'Äänenvoimakkuus: {n}%')
    def led(self,mode):
        cmd=pelta_cli()
        if not cmd:self.status.setText('LED ei käytettävissä: pelta-control CLI puuttuu.');return
        # Only a fixed pair of supported user-requested commands; no guessed raw HID writes.
        try:
            args=[cmd,'light','off'] if mode=='off' else [cmd,'light','static','--color','8000ff','--brightness','100']
            p=subprocess.run(args,capture_output=True,text=True,timeout=8)
            self.status.setText(('LED-komento lähetetty: '+('pois' if mode=='off' else 'päällä')) if p.returncode==0 else 'LED-komento epäonnistui: '+(p.stderr.strip()[:170] or str(p.returncode)))
        except Exception as e:self.status.setText('LED-virhe: '+str(e))
    def tray_click(self,reason):
        if reason==QSystemTrayIcon.ActivationReason.Trigger:self.show_normal()
    def show_normal(self):self.show();self.raise_();self.activateWindow()
    def quit(self):self.meter.stop();self.app.setQuitOnLastWindowClosed(True);self.tray.hide();self.app.quit()
    def closeEvent(self,event):
        if self.tray.isVisible():self.hide();event.ignore()
        else:event.accept()
    def desktop_file(self):
        return Path.home()/'.config/autostart/pelta-control.desktop'
    def has_autostart(self):return self.desktop_file().exists()
    def set_autostart(self,enabled):
        dest=self.desktop_file()
        if enabled:
            dest.parent.mkdir(parents=True,exist_ok=True)
            # AppImage environment supplies APPIMAGE path on launch.
            executable=str(LAUNCHER) if LAUNCHER.exists() else (os.environ.get('APPIMAGE') or os.path.abspath(sys.argv[0]))
            dest.write_text('[Desktop Entry]\nType=Application\nName=Pelta Control\nExec="'+executable.replace('"','\\"')+'" --tray\nIcon=audio-headphones\nTerminal=false\nX-KDE-autostart-after=panel\n')
        else:dest.unlink(missing_ok=True)

def main():
    app=QApplication(sys.argv);app.setQuitOnLastWindowClosed(False)
    client=QLocalSocket()
    client.connectToServer(SINGLE_INSTANCE_NAME)
    if client.waitForConnected(350):
        client.write(b'show')
        client.flush()
        client.waitForBytesWritten(350)
        client.disconnectFromServer()
        return 0
    client.abort()
    server=QLocalServer(app)
    if not server.listen(SINGLE_INSTANCE_NAME):
        probe=QLocalSocket()
        probe.connectToServer(SINGLE_INSTANCE_NAME)
        if probe.waitForConnected(350):
            probe.write(b'show')
            probe.flush()
            probe.waitForBytesWritten(350)
            probe.disconnectFromServer()
            return 0
        probe.abort()
        QLocalServer.removeServer(SINGLE_INSTANCE_NAME)
        if not server.listen(SINGLE_INSTANCE_NAME):
            raise RuntimeError('Pelta Controlin yhden instanssin lukitus epäonnistui')
    w=Window(app)
    def activate_existing():
        while server.hasPendingConnections():
            peer=server.nextPendingConnection()
            peer.readyRead.connect(lambda p=peer: (p.readAll(), w.show_normal(), p.disconnectFromServer()))
            if peer.bytesAvailable():
                peer.readAll()
                w.show_normal()
                peer.disconnectFromServer()
    server.newConnection.connect(activate_existing)
    if '--tray' not in sys.argv:w.show()
    return app.exec()
if __name__=='__main__':sys.exit(main())
