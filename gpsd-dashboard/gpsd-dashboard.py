#!/usr/bin/env python3
import base64
import http.server
import json
import socket
import socketserver
import subprocess

PORT = 80

# Embedded SVG Favicon (Clock + GPS Satellite)
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <!-- Outer Orbit Ring -->
  <path d="M 8 42 A 26 12 35 1 1 56 22" fill="none" stroke="#00bcff" stroke-width="2" stroke-dasharray="3 3" opacity="0.7"/>
  
  <!-- Clock Body -->
  <circle cx="32" cy="32" r="22" fill="#181818" stroke="#00ff00" stroke-width="3"/>
  <circle cx="32" cy="32" r="2" fill="#00ff00"/>
  
  <!-- Clock Ticks -->
  <line x1="32" y1="13" x2="32" y2="16" stroke="#00ff00" stroke-width="2"/>
  <line x1="32" y1="48" x2="32" y2="51" stroke="#00ff00" stroke-width="2"/>
  <line x1="13" y1="32" x2="16" y2="32" stroke="#00ff00" stroke-width="2"/>
  <line x1="48" y1="32" x2="51" y2="32" stroke="#00ff00" stroke-width="2"/>
  
  <!-- Clock Hands -->
  <line x1="32" y1="32" x2="32" y2="20" stroke="#ffffff" stroke-width="2.5" stroke-linecap="round"/>
  <line x1="32" y1="32" x2="40" y2="32" stroke="#00ff00" stroke-width="2" stroke-linecap="round"/>
  
  <!-- Satellite (Top Right Orbit) -->
  <g transform="translate(48, 16) rotate(-25)">
    <!-- Solar Panels -->
    <rect x="-11" y="-3" width="7" height="6" fill="#00bcff" stroke="#ffffff" stroke-width="0.8" rx="1"/>
    <rect x="4" y="-3" width="7" height="6" fill="#00bcff" stroke="#ffffff" stroke-width="0.8" rx="1"/>
    <!-- Satellite Main Body -->
    <rect x="-4" y="-4" width="8" height="8" fill="#ffff00" stroke="#ffffff" stroke-width="0.8" rx="1"/>
  </g>
</svg>"""

FAVICON_BASE64 = base64.b64encode(FAVICON_SVG.encode("utf-8")).decode("utf-8")


def get_chrony_data():
    data = {"tracking": {}, "sources": [], "active_ref_name": "N/A"}

    # Read chronyc sources
    try:
        out = subprocess.check_output(
            ["chronyc", "sources"], text=True, timeout=2
        )
        for line in out.splitlines():
            if len(line) > 2 and line[0] in "#^":
                parts = line.split()
                if len(parts) >= 7:
                    state = line[0:2].strip()
                    name = parts[1]

                    src_entry = {
                        "state": state,
                        "name": name,
                        "stratum": parts[2],
                        "poll": parts[3],
                        "reach": parts[4],
                        "last_rx": parts[5],
                        "last_sample": " ".join(parts[6:]),
                    }
                    data["sources"].append(src_entry)

                    if "*" in state:
                        data["active_ref_name"] = name
    except Exception as e:
        data["sources_error"] = str(e)

    # Read chronyc tracking
    try:
        out = subprocess.check_output(
            ["chronyc", "tracking"], text=True, timeout=2
        )
        for line in out.splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                data["tracking"][key.strip()] = val.strip()

        if data["active_ref_name"] != "N/A":
            data["tracking"]["Reference name"] = data["active_ref_name"]
    except Exception as e:
        data["tracking_error"] = str(e)

    return data


def get_gps_data():
    try:
        s = socket.create_connection(("127.0.0.1", 2947), timeout=2)
        s.sendall(b'?WATCH={"enable":true,"json":true};\n')

        data = {"tpv": {}, "sky": {}}
        for _ in range(15):
            line = s.makefile().readline()
            if not line:
                break
            msg = json.loads(line)
            if msg.get("class") == "TPV":
                data["tpv"] = msg
            elif msg.get("class") == "SKY":
                data["sky"] = msg
            if data["tpv"] and data["sky"]:
                break
        s.close()
        return data
    except Exception as e:
        return {"error": str(e)}


class Handler(http.server.SimpleHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/favicon.ico":
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.end_headers()
            self.wfile.write(FAVICON_SVG.encode("utf-8"))
            return

        if self.path == "/api":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

            combined_data = {
                "gps": get_gps_data(),
                "chrony": get_chrony_data(),
            }
            self.wfile.write(json.dumps(combined_data).encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>NTP & GPSD Combined Dashboard</title>
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,{FAVICON_BASE64}">
    <style>
        body {{ font-family: monospace, sans-serif; background: #121212; color: #00ff00; padding: 20px; margin: 0; }}
        h1 {{ color: #ffffff; border-bottom: 1px solid #333; padding-bottom: 10px; margin-top: 0; display: flex; align-items: center; gap: 12px; }}
        .header-icon {{ width: 36px; height: 36px; vertical-align: middle; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 15px; margin-bottom: 20px; }}
        .card {{ border: 1px solid #333; padding: 15px; background: #181818; border-radius: 5px; }}
        h2 {{ margin-top: 0; color: #00bcff; font-size: 1.1em; border-bottom: 1px solid #222; padding-bottom: 5px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th, td {{ text-align: left; padding: 6px; border-bottom: 1px solid #222; font-size: 0.85em; }}
        th {{ color: #888; }}
        .bar-container {{ background: #222; width: 80px; height: 10px; border-radius: 3px; display: inline-block; overflow: hidden; vertical-align: middle; }}
        .bar {{ height: 100%; background: #00ff00; width: 0%; }}
        .bar.unused {{ background: #555; }}
        .used {{ color: #00ff00; font-weight: bold; }}
        .unused {{ color: #666; }}
        .status-badge {{ padding: 2px 6px; border-radius: 3px; font-weight: bold; font-size: 0.85em; }}
        .fix-3d {{ background: #005500; color: #00ff00; }}
        .fix-none {{ background: #550000; color: #ff5555; }}
        .highlight {{ color: #ffff00; font-weight: bold; }}
    </style>
</head>
<body>
    <h1>
        <img class="header-icon" src="data:image/svg+xml;base64,{FAVICON_BASE64}" alt="Icon">
        NTP & GPSD Dashboard
    </h1>
    <div id="content">Loading data from GPSD and Chrony...</div>

    <script>
        const fixModes = {{ 0: "Unknown", 1: "No Fix", 2: "2D Fix", 3: "3D Fix" }};

        async function update() {{
            try {{
                const res = await fetch('/api');
                const data = await res.json();
                
                const gps = data.gps || {{}};
                const chrony = data.chrony || {{}};
                const tpv = gps.tpv || {{}};
                const sky = gps.sky || {{}};
                const sats = sky.satellites || [];
                const tracking = chrony.tracking || {{}};
                const sources = chrony.sources || [];

                const usedSats = sats.filter(s => s.used).length;
                const fixText = fixModes[tpv.mode] || "No Fix";
                const fixClass = tpv.mode >= 2 ? "fix-3d" : "fix-none";

                let html = `
                <div class="grid">
                    <!-- Chrony Tracking Card -->
                    <div class="card">
                        <h2>Chrony Tracking</h2>
                        <p>Reference Name: <span class="highlight">${{tracking['Reference name'] || chrony.active_ref_name || 'N/A'}}</span></p>
                        <p>Stratum: <span class="highlight">${{tracking['Stratum'] || 'N/A'}}</span></p>
                        <p>System Offset: ${{tracking['Last offset'] || 'N/A'}}</p>
                        <p>RMS Offset: ${{tracking['RMS offset'] || 'N/A'}}</p>
                        <p>Frequency: ${{tracking['Frequency'] || 'N/A'}}</p>
                        <p>Leap Status: ${{tracking['Leap status'] || 'N/A'}}</p>
                    </div>

                    <!-- GPS Fix Card -->
                    <div class="card">
                        <h2>GPS Fix & Status</h2>
                        <p>Status: <span class="status-badge ${{fixClass}}">${{fixText}}</span></p>
                        <p>Visible Satellites: ${{sats.length}}</p>
                        <p>Used Satellites: <span class="used">${{usedSats}}</span></p>
                        <p>Precision: HDOP ${{sky.hdop || 'N/A'}} | PDOP ${{sky.pdop || 'N/A'}}</p>
                        <p>UTC Time: ${{tpv.time || 'N/A'}}</p>
                    </div>

                    <!-- GPS Position Card -->
                    <div class="card">
                        <h2>GPS Position</h2>
                        <p>Latitude: ${{tpv.lat ? tpv.lat.toFixed(6) : 'N/A'}}°</p>
                        <p>Longitude: ${{tpv.lon ? tpv.lon.toFixed(6) : 'N/A'}}°</p>
                        <p>Altitude: ${{tpv.alt ? tpv.alt.toFixed(1) + ' m' : 'N/A'}}</p>
                    </div>
                </div>

                <!-- Chrony Sources Table -->
                <div class="card" style="margin-bottom: 20px;">
                    <h2>Chrony Sources (ntp / refclock)</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>M/S</th>
                                <th>Name / IP</th>
                                <th>Stratum</th>
                                <th>Poll</th>
                                <th>Reach</th>
                                <th>LastRx</th>
                                <th>Last Sample</th>
                            </tr>
                        </thead>
                        <tbody>`;
                
                if (sources.length === 0) {{
                    html += `<tr><td colspan="7">No Chrony sources found (${{chrony.sources_error || 'no error'}})</td></tr>`;
                }} else {{
                    sources.forEach(src => {{
                        const isSelected = src.state.includes('*');
                        const rowStyle = isSelected ? 'style="color: #00ff00; font-weight: bold;"' : '';
                        html += `
                        <tr ${{rowStyle}}>
                            <td>${{src.state}}</td>
                            <td>${{src.name}}</td>
                            <td>${{src.stratum}}</td>
                            <td>${{src.poll}}</td>
                            <td>${{src.reach}}</td>
                            <td>${{src.last_rx}}</td>
                            <td>${{src.last_sample}}</td>
                        </tr>`;
                    }});
                }}

                html += `
                        </tbody>
                    </table>
                </div>

                <!-- GPS Satellites Table -->
                <div class="card">
                    <h2>GPS Satellites (${{sats.length}})</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>PRN / ID</th>
                                <th>Type</th>
                                <th>Elevation</th>
                                <th>Azimuth</th>
                                <th>Signal (dBHz)</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>`;

                if (sats.length === 0) {{
                    html += `<tr><td colspan="6">No satellite signals received</td></tr>`;
                }} else {{
                    sats.sort((a, b) => (b.used - a.used) || ((b.ss || 0) - (a.ss || 0)));
                    sats.forEach(s => {{
                        const prn = s.PRN || s.gnssId || 'N/A';
                        const ss = s.ss || 0;
                        const barWidth = Math.min(Math.max((ss / 50) * 100, 0), 100);
                        const isUsed = s.used;
                        const statusText = isUsed ? '<span class="used">Used</span>' : '<span class="unused">Ignored</span>';
                        const barClass = isUsed ? 'bar' : 'bar unused';

                        html += `
                        <tr>
                            <td><strong>#${{prn}}</strong></td>
                            <td>${{s.gnssId || 'GPS'}}</td>
                            <td>${{s.el || 0}}°</td>
                            <td>${{s.az || 0}}°</td>
                            <td>
                                <div class="bar-container">
                                    <div class="${{barClass}}" style="width: ${{barWidth}}%;"></div>
                                </div>
                                <span style="margin-left: 5px;">${{ss}} dBHz</span>
                            </td>
                            <td>${{statusText}}</td>
                        </tr>`;
                    }});
                }}

                html += `
                        </tbody>
                    </table>
                </div>`;

                document.getElementById('content').innerHTML = html;
            }} catch (err) {{
                document.getElementById('content').innerHTML = `<div class="card" style="color:red;">Connection error to dashboard server</div>`;
            }}
        }}

        update();
        setInterval(update, 3000);
    </script>
</body>
</html>"""
        self.wfile.write(html.encode("utf-8"))


if __name__ == "__main__":
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Combined Dashboard listening on port {PORT}")
        httpd.serve_forever()

