# FundiLoop — the AI pair-programmer for hardware.

A hands-free diagnostic loop for dead low-voltage DC circuits: the tech says
"check now", the system grabs one frame, segments it, reads real I2C sensor
data from the patient board over USB serial, and Muse Spark returns a
structured next step. The backend runs the verification test itself — the
model never declares success, the sensor does.

## Run it

1. `pip install -r backend/requirements.txt`
2. `export MODEL_API_KEY=...` (PowerShell: `$env:MODEL_API_KEY="..."`)
3. `export SERIAL_PORT=/dev/ttyUSB0` (Windows: `COM3` or similar)
4. `uvicorn backend.main:app --host 0.0.0.0 --port 8000`
5. Open `http://<laptop-ip>:8000` on your phone.
