import os
from dotenv import load_dotenv

load_dotenv()

KEEPA_API_KEY = os.getenv("KEEPA_API_KEY", "")
DISTRIBUTOR_MOQ = float(os.getenv("DISTRIBUTOR_MOQ", 3000.00))
MIN_BRAND_UNITS = int(os.getenv("MIN_BRAND_UNITS", 12))
MAX_AMAZON_1P_SHARE = float(os.getenv("MAX_AMAZON_BUYBOX_SHARE", 0.15))
MAX_BOX_WEIGHT_LBS = float(os.getenv("MAX_BOX_WEIGHT_LBS", 49.0))
