"""
Video Thumbnail Contact Sheet Generator
---------------------------------------

Creates a grid of thumbnails from a video:
- 4 columns
- 8 rows
- 32 evenly spaced frames total

Requirements:
    pip install opencv-python pillow numpy

Usage:
    python thumbnail_grid.py input_video.mp4 output.jpg
"""

import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# =========================
# CONFIG
# =========================
COLUMNS = 4
ROWS = 8
THUMB_WIDTH = 320
THUMB_HEIGHT = 180
PADDING = 8
TIMESTAMP_BAR_HEIGHT = 28
BACKGROUND_COLOR = (20, 20, 20)
TEXT_COLOR = (255, 255, 255)

TOTAL_FRAMES = COLUMNS * ROWS


def seconds_to_timestamp(seconds):
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if hrs > 0:
        return f"{hrs:02}:{mins:02}:{secs:02}"
    return f"{mins:02}:{secs:02}"


def extract_frame(video, time_sec):
    """
    Extract frame from video at specific time (seconds)
    """
    video.set(cv2.CAP_PROP_POS_MSEC, time_sec * 1000)
    success, frame = video.read()

    if not success:
        return None

    # Convert BGR -> RGB
    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    return frame


def create_thumbnail(frame):
    """
    Resize frame into thumbnail
    """
    img = Image.fromarray(frame)
    img = img.resize((THUMB_WIDTH, THUMB_HEIGHT), Image.LANCZOS)
    return img


def draw_timestamp(img, timestamp):
    """
    Add timestamp overlay
    """
    draw = ImageDraw.Draw(img)

    bar_y = THUMB_HEIGHT - TIMESTAMP_BAR_HEIGHT

    # Semi-transparent black rectangle
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    overlay_draw.rectangle([(0, bar_y), (THUMB_WIDTH, THUMB_HEIGHT)], fill=(0, 0, 0, 160))

    img = Image.alpha_composite(img.convert("RGBA"), overlay)

    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except:
        font = ImageFont.load_default()

    text_bbox = draw.textbbox((0, 0), timestamp, font=font)
    text_width = text_bbox[2] - text_bbox[0]

    x = THUMB_WIDTH - text_width - 10
    y = bar_y + 4

    draw.text((x, y), timestamp, fill=TEXT_COLOR, font=font)

    return img.convert("RGB")


def generate_contact_sheet(video_path, output_path):
    video = cv2.VideoCapture(video_path)

    if not video.isOpened():
        print("Error: Could not open video.")
        return

    fps = video.get(cv2.CAP_PROP_FPS)
    frame_count = int(video.get(cv2.CAP_PROP_FRAME_COUNT))

    duration = frame_count / fps

    print(f"Video duration: {duration:.2f} seconds")

    # Canvas size
    sheet_width = COLUMNS * THUMB_WIDTH + (COLUMNS + 1) * PADDING

    sheet_height = ROWS * THUMB_HEIGHT + (ROWS + 1) * PADDING

    sheet = Image.new("RGB", (sheet_width, sheet_height), BACKGROUND_COLOR)

    # Evenly spaced timestamps
    timestamps = np.linspace(0, duration, TOTAL_FRAMES + 2)[1:-1]

    for idx, ts in enumerate(timestamps):
        print(f"Processing thumbnail {idx + 1}/{TOTAL_FRAMES}")

        frame = extract_frame(video, ts)

        if frame is None:
            continue

        thumb = create_thumbnail(frame)

        timestamp_text = seconds_to_timestamp(ts)
        thumb = draw_timestamp(thumb, timestamp_text)

        row = idx // COLUMNS
        col = idx % COLUMNS

        x = PADDING + col * (THUMB_WIDTH + PADDING)
        y = PADDING + row * (THUMB_HEIGHT + PADDING)

        sheet.paste(thumb, (x, y))

    sheet.save(output_path, quality=95)

    video.release()

    print(f"\nSaved thumbnail sheet to: {output_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("\nUsage:")
        print("python thumbnail_grid.py input_video.mp4 output.jpg\n")
        sys.exit(1)

    input_video = sys.argv[1]
    output_image = sys.argv[2]

    generate_contact_sheet(input_video, output_image)
