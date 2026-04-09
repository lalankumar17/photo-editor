import os
from flask import Flask, request, render_template, send_file, jsonify
from PIL import Image, ImageOps
from io import BytesIO
import requests
import traceback

app = Flask(__name__)


def load_local_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


load_local_env()

# ─────────────────────────────────────────────────────────────
# remove.bg API — free background removal (50 calls/month free)
# Sign up at https://www.remove.bg/api and add REMOVE_BG_API_KEY
# to your Render environment variables.
# ─────────────────────────────────────────────────────────────
REMOVE_BG_KEY = os.environ.get("REMOVE_BG_API_KEY", "").strip()
REMOVE_BG_SERVER_KEY_DISABLED = False
REMOVE_BG_QUOTA_HINTS = (
    "credit",
    "credits",
    "quota",
    "payment required",
    "free preview",
    "free previews",
    "insufficient",
    "out of credits",
)
REMOVE_BG_KEY_HINTS = (
    "api key",
    "invalid",
    "unauthorized",
    "authentication",
    "forbidden",
)
if REMOVE_BG_KEY:
    print("[OK] remove.bg API key found - background removal enabled.")
else:
    print("[WARN] REMOVE_BG_API_KEY not set. Background removal will fail. "
          "Add the key in Render -> Environment.")

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
    print("[OK] Cloudinary AI enhancement: ENABLED")
else:
    print("[INFO] Cloudinary keys not set - AI enhancement skipped.")


class RemoveBgApiError(Exception):
    def __init__(self, message: str, status_code: int = 500, requires_user_api_key: bool = False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.requires_user_api_key = requires_user_api_key

    def to_dict(self) -> dict:
        payload = {"error": self.message}
        if self.requires_user_api_key:
            payload["requires_user_api_key"] = True
        return payload


def get_remove_bg_error_message(resp: requests.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        data = None

    messages = []
    if isinstance(data, dict):
        error_items = data.get("errors")
        if isinstance(error_items, list):
            for item in error_items:
                if not isinstance(item, dict):
                    continue
                for key in ("title", "detail", "message", "code"):
                    value = item.get(key)
                    if value:
                        messages.append(str(value))
        for key in ("message", "error", "detail"):
            value = data.get(key)
            if value:
                messages.append(str(value))

    if not messages and resp.text:
        messages.append(resp.text[:300])

    message = " ".join(part.strip() for part in messages if part and str(part).strip())
    return " ".join(message.split()) or f"HTTP {resp.status_code}"


@app.route("/")
def index():
    return render_template(
        "index.html",
        server_remove_bg_key_available=bool(REMOVE_BG_KEY) and not REMOVE_BG_SERVER_KEY_DISABLED,
    )


def remove_background(image_bytes: bytes, api_key: str = None) -> bytes:
    """
    Call remove.bg REST API to strip the background.
    Returns PNG bytes with transparent background.
    Zero ML libraries — works within Render's 512 MB free tier.
    """
    global REMOVE_BG_SERVER_KEY_DISABLED

    using_user_key = bool(api_key)
    key_to_use = api_key if using_user_key else ("" if REMOVE_BG_SERVER_KEY_DISABLED else REMOVE_BG_KEY)
    if not using_user_key and REMOVE_BG_SERVER_KEY_DISABLED:
        raise RemoveBgApiError(
            "Built-in remove.bg quota is finished. Enter your own remove.bg API key to continue.",
            status_code=402,
            requires_user_api_key=True,
        )
    if not key_to_use:
        raise RemoveBgApiError(
            "Server remove.bg key is not configured. Enter your own remove.bg API key to continue.",
            status_code=402,
            requires_user_api_key=True,
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

    error_msg = get_remove_bg_error_message(resp)
    error_msg_lower = error_msg.lower()
    print(f"ERROR: remove.bg API returned {resp.status_code}: {error_msg}")

    quota_exhausted = resp.status_code == 402 or any(hint in error_msg_lower for hint in REMOVE_BG_QUOTA_HINTS)
    key_unavailable = resp.status_code in (401, 403) or any(hint in error_msg_lower for hint in REMOVE_BG_KEY_HINTS)

    if not using_user_key and (quota_exhausted or key_unavailable):
        REMOVE_BG_SERVER_KEY_DISABLED = True
        if quota_exhausted:
            raise RemoveBgApiError(
                "Built-in remove.bg quota is finished. Enter your own remove.bg API key to continue.",
                status_code=402,
                requires_user_api_key=True,
            )
        raise RemoveBgApiError(
            "Built-in remove.bg key is unavailable right now. Enter your own remove.bg API key to continue.",
            status_code=402,
            requires_user_api_key=True,
        )

    if resp.status_code == 429:
        raise RemoveBgApiError(
            "remove.bg rate limit reached. Please try again in a moment.",
            status_code=429,
        )

    raise RemoveBgApiError(
        f"Background removal failed (HTTP {resp.status_code}). Details: {error_msg}",
        status_code=500,
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
            print(f"WARNING: Cloudinary enhancement failed - using remove.bg result. {e}")

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
        except RemoveBgApiError as e:
            print(f"ERROR: image {idx + 1}: {e.message}")
            return jsonify(e.to_dict()), e.status_code
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
    debug_mode = os.environ.get("FLASK_DEBUG", "").strip() == "1"
    app.run(host="0.0.0.0", port=port, debug=debug_mode, use_reloader=debug_mode)
