"""High-quality visual schematics for tutoring mode.

This module generates a realistic breadboard-style layout for ESP32 projects,
specifically tailored for the Smart Distance-Activated Servo System.
"""
from __future__ import annotations
from xml.sax.saxutils import escape
import re

# Wire colors matching the user's diagram
COLORS = {
    "power": "#ef4444",   # Red
    "ground": "#4b5563",  # Grey/Black
    "trig": "#3b82f6",    # Blue
    "echo": "#22c55e",    # Green
    "signal": "#a855f7",  # Purple
}

def render_visual_svg(netlist: dict, step_n: int) -> str:
    width, height = 800, 600
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
        '<rect width="100%" height="100%" fill="#0a0a0c" />', # Dark background to match UI
        
        # Define some filters and gradients for glassmorphism
        '<defs>',
        '  <filter id="blur"><feGaussianBlur stdDeviation="5"/></filter>',
        '  <linearGradient id="grad-esp32" x1="0%" y1="0%" x2="100%" y2="100%">',
        '    <stop offset="0%" style="stop-color:#1e1e2e;stop-opacity:1" />',
        '    <stop offset="100%" style="stop-color:#0f0f1a;stop-opacity:1" />',
        '  </linearGradient>',
        '</defs>',

        # 1. The Breadboard (Base layer)
        '<g id="breadboard" opacity="0.8">',
        '  <rect x="250" y="100" width="300" height="400" rx="10" fill="#f8f9fa" stroke="#d1d5db" stroke-width="2"/>',
        # Draw holes
        *[f'<circle cx="{270 + i*10}" cy="{120 + j*10}" r="1.5" fill="#d1d5db"/>' for i in range(27) for j in range(37) if not (12 <= i <= 14)],
        '  <line x1="260" y1="110" x2="260" y2="490" stroke="#ef4444" stroke-width="1" stroke-dasharray="2,2"/>', # Power rail
        '  <line x1="280" y1="110" x2="280" y2="490" stroke="#3b82f6" stroke-width="1" stroke-dasharray="2,2"/>', # Ground rail
        '</g>',

        # 2. ESP32 DevKit v1 (The Brain)
        '<g id="part-esp32" transform="translate(325, 150)">',
        '  <rect width="150" height="250" rx="5" fill="url(#grad-esp32)" stroke="#3b82f6" stroke-width="2"/>',
        '  <text x="75" y="40" font-family="sans-serif" font-size="12" fill="white" text-anchor="middle" font-weight="bold">ESP32 DevKit v1</text>',
        '  <rect x="60" y="-10" width="30" height="20" fill="#333" rx="2"/>', # USB Port
        # Pins
        *[f'<rect x="0" y="{60 + i*15}" width="10" height="5" fill="#aab2bd"/>' for i in range(12)],
        *[f'<rect x="140" y="{60 + i*15}" width="10" height="5" fill="#aab2bd"/>' for i in range(12)],
        '</g>',
    ]

    # Components and wires based on steps
    wires = netlist.get("wires", [])
    parts_requested = netlist.get("requested", [])

    # Function to draw a wire with progressive highlight
    def draw_wire(wire_id, path_d, signal_type, step_added):
        fresh = step_added == step_n
        opacity = "1" if fresh else "0.3"
        color = COLORS.get(signal_type.lower(), COLORS["signal"])
        width = 3 if fresh else 2
        return f'<path id="{wire_id}" d="{path_d}" stroke="{color}" stroke-width="{width}" fill="none" opacity="{opacity}" />'

    # Step 1: Power Hub
    if step_n >= 1:
        out.append(
            '<g id="part-power" transform="translate(50, 50)">'
            '  <rect width="120" height="100" rx="10" fill="rgba(255,255,255,0.05)" stroke="rgba(255,255,255,0.1)"/>'
            '  <text x="60" y="30" font-family="sans-serif" font-size="10" fill="#9ca3af" text-anchor="middle">USB-C 5V Adapter</text>'
            '  <path d="M40 50 L80 50 M60 40 L60 60" stroke="#9ca3af" stroke-width="2"/>'
            '</g>'
        )
        # Power wires (Step 1 usually involves power)
        out.append(draw_wire("wire-pwr-5v", "M170 100 Q 210 100, 250 120", "power", 1))
        out.append(draw_wire("wire-pwr-gnd", "M170 120 Q 210 120, 250 140", "ground", 1))

    # Step 3: The Eyes (Ultrasonic Sensor)
    if "hc_sr04" in parts_requested:
        sensor_step = 3 # Default for this project
        sensor_opacity = "1" if step_n >= sensor_step else "0.2"
        out.append(
            f'<g id="part-sensor" transform="translate(50, 400)" opacity="{sensor_opacity}">'
            '  <rect width="120" height="100" rx="10" fill="rgba(255,255,255,0.05)" stroke="rgba(255,255,255,0.1)"/>'
            '  <text x="60" y="20" font-family="sans-serif" font-size="10" fill="#9ca3af" text-anchor="middle">HC-SR04P Sensor</text>'
            '  <circle cx="35" cy="60" r="20" stroke="#9ca3af" fill="none" stroke-width="2"/>'
            '  <circle cx="85" cy="60" r="20" stroke="#9ca3af" fill="none" stroke-width="2"/>'
            '</g>'
        )
        if step_n >= sensor_step:
            out.append(draw_wire("wire-sensor-vcc", "M170 420 Q 210 420, 250 120", "power", sensor_step))
            out.append(draw_wire("wire-sensor-gnd", "M170 440 Q 210 440, 250 140", "ground", sensor_step))
            out.append(draw_wire("wire-sensor-trig", "M170 460 Q 300 460, 325 355", "trig", sensor_step))
            out.append(draw_wire("wire-sensor-echo", "M170 480 Q 300 480, 325 370", "echo", sensor_step))

    # Step 4: The Muscle (Servo)
    if "servo" in parts_requested:
        servo_step = 4 # Default for this project
        servo_opacity = "1" if step_n >= servo_step else "0.2"
        out.append(
            f'<g id="part-servo" transform="translate(630, 50)" opacity="{servo_opacity}">'
            '  <rect width="120" height="100" rx="10" fill="rgba(255,255,255,0.05)" stroke="rgba(255,255,255,0.1)"/>'
            '  <text x="60" y="20" font-family="sans-serif" font-size="10" fill="#9ca3af" text-anchor="middle">SG90 Servo</text>'
            '  <rect x="30" y="40" width="60" height="40" fill="#3b82f6" rx="2"/>'
            '  <circle cx="60" cy="50" r="10" fill="white"/>'
            '</g>'
        )
        if step_n >= servo_step:
            out.append(draw_wire("wire-servo-vcc", "M630 70 Q 590 70, 550 120", "power", servo_step))
            out.append(draw_wire("wire-servo-gnd", "M630 90 Q 590 90, 550 140", "ground", servo_step))
       