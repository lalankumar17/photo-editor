import os
from flask import Flask, request, render_template, send_file, jsonify
from PIL import Image, ImageOps
from io import BytesIO
import requests
import traceback

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────
# remove.bg API — free background removal (50 calls/month free)
# Sign up at https://www.remove.bg/api and add REMOVE_BG_API_KEY
# to your Render environment variables.
# ─────────────────────────────────────────────────────────────
REMOVE_BG_KEY = os.environ.get("REMOVE_BG_API_KEY", "").strip()
if REMOVE_BG_KEY:
    print("✅ remove.bg API key found — background removal enabled.")
else:
    print("⚠️  REMOVE_BG_API_KEY not set. Background removal will fail. "
          "Add the key in Render → Environment.")

# ─────────────────────────────────────────────────────────────
# Cloudinary (optional) — AI enhancement
# ─────────────────────────────────────────────────────────────
CLOUDINARY_ENABLED = all([
    os.getenv("CLOUDINARY_CLOUD_NAME"),
    os.getenv("CLOUDINARY_API_KEY"),
    os.getenv("CLOUDINARY_API_SECRET"),
])
if CLOUDINARY_ENABLED:
    import cloudinary
    import cloudinary.uploader
    import cloudinary.utils
    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
        api_key=os.getenv("CLOUDINARY_API_KEY"),
        api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    )
    print("✅ Cloudinary AI enhancement: ENABLED")
else:
    print("ℹ️  Cloudinary keys not set — AI enhancement skipped.")


@app.route("/")
def index():
    return render_template("index.html")


def remove_background(image_bytes: bytes, api_key: str = None) -> bytes:
    """
    Call remove.bg REST API to strip the background.
    Returns PNG bytes with transparent background.
    Zero ML libraries — works within Render's 512 MB free tier.
    """
    key_to_use = api_key if api_key else REMOVE_BG_KEY
    if not key_to_use:
        raise RuntimeError(
            "REMOVE_BG_API_KEY is not configured. "
            "Please provide your own API key or configure it in the server environment."
        )

    resp = requests.post(
        "https://api.remove.bg/v1.0/removebg",
        files={"image_file": ("photo.jpg", image_bytes, "image/jpeg")},
        data={"size": "auto"},
        headers={"X-Api-Key": key_to_use},
        timeout=60,
    )

    if resp.status_code == 200:
        return resp.content  # PNG with transparent background
    else:
        error_msg = resp.text[:300]
        print(f"ERROR: remove.bg API returned {resp.status_code}: {error_msg}")
        raise RuntimeError(
            f"Background removal failed (HTTP {resp.status_code}). "
            f"Details: {error_msg}"
        )


def process_single_image(input_image_bytes: bytes, bg_color_hex: str = "#ffffff", api_key: str = None) -> Image.Image:
    """Remove background via API, apply bg color, optionally Cloudinary-enhance."""

    # Parse hex → RGB
    hex_clean = bg_color_hex.lstrip("#")
    if len(hex_clean) == 6:
        bg_color_rgb = tuple(int(hex_clean[i:i+2], 16) for i in (0, 2, 4))
    else:
        bg_color_rgb = (255, 255, 255)

    # Step 1: Background removal
    bg_removed_bytes = remove_background(input_image_bytes, api_key)
    img = Image.open(BytesIO(bg_removed_bytes))

    # Paste onto chosen background colour
    if img.mode in ("RGBA", "LA"):
        background = Image.new("RGB", img.size, bg_color_rgb)
        background.paste(img, mask=img.split()[-1])
        processed_img = background
    else:
        processed_img = img.convert("RGB")

    # Step 2 (Optional): Cloudinary AI enhancement
    if CLOUDINARY_ENABLED:
        try:
            buf = BytesIO()
            processed_img.save(buf, format="PNG")
            buf.seek(0)
            upload_result = cloudinary.uploader.upload(buf, resource_type="image")
            public_id = upload_result.get("public_id")
            if public_id:
                enhanced_url = cloudinary.utils.cloudinary_url(
                    public_id,
                    transformation=[
                        {"effect": "gen_restore"},
                        {"quality": "auto"},
                        {"fetch_format": "auto"},
                    ],
                )[0]
                enhanced_data = requests.get(enhanced_url, timeout=30).content
                img2 = Image.open(BytesIO(enhanced_data))
                if img2.mode in ("RGBA", "LA"):
                    bg2 = Image.new("RGB", img2.size, (255, 255, 255))
                    bg2.paste(img2, mask=img2.split()[-1])
                    processed_img = bg2
                else:
                    processed_img = img2.convert("RGB")

        except Exception as e:
            print(f"WARNING: Cloudinary enhancement failed — using remove.bg result. {e}")

    return processed_img


@app.route("/process", methods=["POST"])
def process():
    passport_width  = int(request.form.get("width",   390))
    passport_height = int(request.form.get("height",  480))
    border          = int(request.form.get("border",    2))
    spacing         = int(request.form.get("spacing",  10))
    bg_color        = request.form.get("bg_color", "#ffffff")
    user_api_key    = request.form.get("remove_bg_key", "").strip()
    margin_x        = 10
    margin_y        = 10
    horizontal_gap  = 10
    a4_w, a4_h      = 2480, 3508

    # Collect images
    images_data = []
    i = 0
    while f"image_{i}" in request.files:
        file   = request.files[f"image_{i}"]
        copies = int(request.form.get(f"copies_{i}", 6))
        images_data.append((file.read(), copies))
        i += 1

    if not images_data and "image" in request.files:
        file   = request.files["image"]
        copies = int(request.form.get("copies", 6))
        images_data.append((file.read(), copies))

    if not images_data:
        return jsonify({"error": "No image uploaded"}), 400

    passport_images = []
    for idx, (img_bytes, copies) in enumerate(images_data):
        try:
            img = process_single_image(img_bytes, bg_color, user_api_key)
            img = img.resize((passport_width, passport_height), Image.LANCZOS)
            img = ImageOps.expand(img, border=border, fill="black")
            passport_images.append((img, copies))
        except Exception as e:
            print(f"ERROR: image {idx + 1}: {e}")
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    paste_w = passport_width  + 2 * border
    paste_h = passport_height + 2 * border

    pages        = []
    current_page = Image.new("RGB", (a4_w, a4_h), "white")
    x, y         = margin_x, margin_y

    def new_page():
        nonlocal current_page, x, y
        pages.append(current_page)
        current_page = Image.new("RGB", (a4_w, a4_h), "white")
        x, y = margin_x, margin_y

    for passport_img, copies in passport_images:
        for _ in range(copies):
            if x + paste_w > a4_w - margin_x:
                x  = margin_x
                y += paste_h + spacing
            if y + paste_h > a4_h - margin_y:
                new_page()
            current_page.paste(passport_img, (x, y))
            x += paste_w + horizontal_gap

    pages.append(current_page)

    output = BytesIO()
    if len(pages) == 1:
        pages[0].save(output, format="PDF", dpi=(300, 300))
    else:
        pages[0].save(
            output,
            format="PDF",
            dpi=(300, 300),
            save_all=True,
            append_images=pages[1:],
        )
    output.seek(0)

    return send_file(
        output,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="passport-sheet.pdf",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)