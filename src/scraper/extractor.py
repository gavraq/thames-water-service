"""Thames Water consumption data extractor using Selenium."""

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, WebDriverException

from src.config import get_settings
from src.database.models import DailyUsage, HourlyUsage
from src.scraper.exceptions import (
    LoginError,
    NavigationError,
    ExtractionError,
)
from src.scraper.parser import (
    parse_date_label,
    parse_month_year,
    parse_daily_consumption,
    validate_daily_usage,
)
from src.utils.logger import get_logger
from src.utils.retry import retry_with_backoff

logger = get_logger(__name__)


class ThamesWaterExtractor:
    """Extract water consumption data from Thames Water smart meter portal."""

    LOGIN_URL = "https://www.thameswater.co.uk/login"
    ACCOUNT_URL = "https://www.thameswater.co.uk/my-account/overview"
    USAGE_URL = "https://www.thameswater.co.uk/my-account/usage"

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        headless: bool | None = None,
    ):
        """
        Initialize extractor.

        Args:
            email: Thames Water account email (defaults to config)
            password: Thames Water account password (defaults to config)
            headless: Run browser in headless mode (defaults to config)
        """
        settings = get_settings()
        self.email = email or settings.thames_water_email
        self.password = password or settings.thames_water_password
        self.headless = headless if headless is not None else settings.scraper_headless
        self.timeout = settings.scraper_timeout
        self.driver: webdriver.Chrome | None = None

    def _setup_driver(self) -> None:
        """Configure and create Chrome WebDriver."""
        logger.info("Setting up Chrome WebDriver", extra={"headless": self.headless})

        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-gpu")

        # Use pre-installed chromium in Docker container (seleniarm base image)
        chromium_path = Path("/usr/bin/chromium")
        chromedriver_path = Path("/usr/bin/chromedriver")

        if chromium_path.exists():
            options.binary_location = str(chromium_path)
            logger.info("Using chromium binary", extra={"path": str(chromium_path)})

        # Enable performance logging to capture network requests
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        # Create service with explicit chromedriver path if available
        if chromedriver_path.exists():
            service = Service(executable_path=str(chromedriver_path))
            logger.info("Using chromedriver", extra={"path": str(chromedriver_path)})
            self.driver = webdriver.Chrome(service=service, options=options)
        else:
            self.driver = webdriver.Chrome(options=options)

        self.driver.implicitly_wait(10)

    def _handle_cookie_consent(self) -> bool:
        """Dismiss cookie consent popup if present."""
        try:
            wait = WebDriverWait(self.driver, 5)
            cookie_selectors = [
                (By.ID, "onetrust-accept-btn-handler"),
                (By.CLASS_NAME, "onetrust-close-btn-handler"),
                (By.XPATH, "//button[contains(text(), 'Accept')]"),
                (By.XPATH, "//button[contains(text(), 'Accept All')]"),
            ]

            for selector in cookie_selectors:
                try:
                    btn = wait.until(EC.element_to_be_clickable(selector))
                    btn.click()
                    logger.info("Dismissed cookie consent popup")
                    time.sleep(1)
                    return True
                except TimeoutException:
                    continue

        except Exception:
            pass

        return False

    @retry_with_backoff(max_retries=2, base_delay=5.0, exceptions=(LoginError,))
    def _login(self) -> bool:
        """Log into Thames Water account."""
        logger.info("Navigating to Thames Water login")
        self.driver.get(self.LOGIN_URL)

        try:
            time.sleep(3)
            self._handle_cookie_consent()
            time.sleep(1)

            wait = WebDriverWait(self.driver, self.timeout)

            # Find email field
            email_selectors = [
                (By.ID, "signInName"),
                (By.ID, "email"),
                (By.NAME, "email"),
                (By.CSS_SELECTOR, "input[type='email']"),
                (By.XPATH, "//input[@placeholder='Email Address']"),
            ]

            email_field = None
            for selector in email_selectors:
                try:
                    email_field = wait.until(EC.presence_of_element_located(selector))
                    logger.debug(f"Found email field with selector: {selector}")
                    break
                except TimeoutException:
                    continue

            if not email_field:
                self._save_debug_screenshot("login_no_email_field")
                raise LoginError("Could not find email field")

            email_field.clear()
            email_field.send_keys(self.email)

            # Find password field
            password_selectors = [
                (By.ID, "password"),
                (By.NAME, "password"),
                (By.CSS_SELECTOR, "input[type='password']"),
            ]

            password_field = None
            for selector in password_selectors:
                try:
                    password_field = self.driver.find_element(*selector)
                    break
                except Exception:
                    continue

            if not password_field:
                raise LoginError("Could not find password field")

            password_field.clear()
            password_field.send_keys(self.password)

            # Find and click sign in button
            sign_in_selectors = [
                (By.ID, "next"),
                (By.CSS_SELECTOR, "button[type='submit']"),
                (By.XPATH, "//button[contains(text(), 'Sign in')]"),
            ]

            for selector in sign_in_selectors:
                try:
                    sign_in_btn = self.driver.find_element(*selector)
                    sign_in_btn.click()
                    break
                except Exception:
                    continue

            logger.info("Submitted login form, waiting for redirect")
            time.sleep(8)

            # Check if login was successful
            current_url = self.driver.current_url
            success_indicators = [
                "my-account" in current_url,
                "mydashboard" in current_url,
                "overview" in current_url,
            ]

            if any(success_indicators):
                logger.info("Login successful", extra={"url": current_url})
                return True

            # Try navigating to account page directly
            self.driver.get(self.ACCOUNT_URL)
            time.sleep(5)

            current_url = self.driver.current_url
            if "my-account" in current_url or "overview" in current_url:
                logger.info("Login successful after navigation")
                return True

            self._save_debug_screenshot("login_failed")
            raise LoginError(f"Login failed, ended at URL: {current_url}")

        except LoginError:
            raise
        except Exception as e:
            self._save_debug_screenshot("login_error")
            raise LoginError(f"Login error: {e}") from e

    def _navigate_to_water_use(self) -> bool:
        """Navigate to the water usage page."""
        logger.info("Navigating to water usage page")

        try:
            self.driver.get(self.USAGE_URL)
            time.sleep(5)

            current_url = self.driver.current_url
            if "usage" not in current_url.lower():
                raise NavigationError(f"Not on usage page: {current_url}")

            # Click "View water usage" link
            try:
                wait = WebDriverWait(self.driver, 10)
                link_selectors = [
                    (By.LINK_TEXT, "View water usage"),
                    (By.PARTIAL_LINK_TEXT, "View water"),
                    (By.XPATH, "//a[contains(@href, 'water-usage')]"),
                ]

                for selector in link_selectors:
                    try:
                        link = wait.until(EC.element_to_be_clickable(selector))
                        link.click()
                        logger.info("Clicked 'View water usage' link")
                        time.sleep(5)
                        break
                    except TimeoutException:
                        continue

            except Exception as e:
                logger.warning(f"Could not click View water usage: {e}")

            return True

        except Exception as e:
            self._save_debug_screenshot("navigation_error")
            raise NavigationError(f"Navigation error: {e}") from e

    def _set_daily_view(self) -> bool:
        """Set the view to 'Monthly (by days)' to get daily data."""
        try:
            selects = self.driver.find_elements(By.TAG_NAME, "select")

            for sel in selects:
                try:
                    select_obj = Select(sel)
                    options = [o.text for o in select_obj.options]

                    if "Monthly (by days)" in options:
                        select_obj.select_by_visible_text("Monthly (by days)")
                        logger.info("Set view to 'Monthly (by days)'")
                        time.sleep(3)
                        return True
                except Exception:
                    continue

            return False

        except Exception as e:
            logger.warning(f"Could not set daily view: {e}")
            return False

    def _select_month(self, month_str: str) -> bool:
        """Select a specific month from the dropdown."""
        try:
            selects = self.driver.find_elements(By.TAG_NAME, "select")

            for sel in selects:
                try:
                    select_obj = Select(sel)
                    options = [o.text for o in select_obj.options]
                    logger.debug(f"Dropdown options: {options}")

                    # Try exact match first
                    if month_str in options:
                        select_obj.select_by_visible_text(month_str)
                        logger.info(f"Selected: {month_str}")
                        time.sleep(4)
                        return True

                    # Try "Last 30 days" for current month
                    if month_str == "Last 30 days" and "Last 30 days" in options:
                        select_obj.select_by_visible_text("Last 30 days")
                        logger.info("Selected: Last 30 days")
                        time.sleep(4)
                        return True

                    # Try alternative formats (e.g., "December 2025" vs "Dec-2025")
                    for opt in options:
                        # Check if month name matches
                        if month_str.replace("-", " ") in opt or opt.replace(" ", "-") == month_str:
                            select_obj.select_by_visible_text(opt)
                            logger.info(f"Selected alternative format: {opt}")
                            time.sleep(4)
                            return True
                except Exception:
                    continue

            logger.warning(f"Could not find month in dropdowns: {month_str}")
            return False

        except Exception as e:
            logger.warning(f"Error selecting month: {e}")
            return False

    def _extract_from_logs(self) -> list[dict[str, Any]]:
        """Extract consumption data from browser performance logs."""
        logs = self.driver.get_log("performance")
        extracted_data = []

        for entry in logs:
            try:
                log_data = json.loads(entry["message"])["message"]

                if log_data["method"] == "Network.responseReceived":
                    url = log_data["params"]["response"]["url"]

                    if "consumption" in url.lower() or "GetSmartWaterMeterConsumptions" in url:
                        request_id = log_data["params"]["requestId"]

                        try:
                            response = self.driver.execute_cdp_cmd(
                                "Network.getResponseBody",
                                {"requestId": request_id}
                            )
                            body = response.get("body", "")
                            if body:
                                data = json.loads(body)
                                if isinstance(data, dict) and data.get("Lines"):
                                    extracted_data.append(data)
                                    logger.debug(f"Extracted data with {len(data['Lines'])} lines")
                        except Exception:
                            pass

            except Exception:
                continue

        return extracted_data

    def _parse_daily_data(
        self,
        raw_data: list[dict[str, Any]],
        month_str: str,
    ) -> list[DailyUsage]:
        """Parse raw data into DailyUsage objects."""
        records = []
        month_info = parse_month_year(month_str)

        # For "Last 30 days" or other non-month strings, use current year
        # and infer year from label month vs current month
        current_year = datetime.now().year
        current_month = datetime.now().month

        if not month_info:
            logger.info(f"Using date inference for: {month_str}")
            # Will infer year per record based on month name
            year = None
        else:
            _, year = month_info

        for data_set in raw_data:
            lines = data_set.get("Lines", [])
            for line in lines:
                label = line.get("Label", "")

                # Infer year if not provided
                inferred_year = year
                if not inferred_year:
                    # Labels are like "15-November" or "3-December"
                    # If the month in the label is > current month, it's from last year
                    month_names = {
                        "January": 1, "February": 2, "March": 3, "April": 4,
                        "May": 5, "June": 6, "July": 7, "August": 8,
                        "September": 9, "October": 10, "November": 11, "December": 12
                    }
                    for month_name, month_num in month_names.items():
                        if month_name in label:
                            # If label month > current month, it's last year
                            if month_num > current_month:
                                inferred_year = current_year - 1
                            else:
                                inferred_year = current_year
                            break
                    if not inferred_year:
                        inferred_year = current_year

                date = parse_date_label(label, inferred_year)

                if date:
                    usage = DailyUsage(
                        date=date,
                        usage_litres=float(line.get("Usage", 0)),
                        meter_reading=float(line.get("Read", 0)) if line.get("Read") else None,
                        is_estimated=bool(line.get("IsEstimated", False)),
                        source="scraper",
                    )

                    if validate_daily_usage(usage):
                        records.append(usage)

        return records

    def extract_month(self, month_str: str) -> list[DailyUsage]:
        """
        Extract daily usage data for a specific month.

        Args:
            month_str: Month string like "Dec-2024" or "Nov-2025"

        Returns:
            List of DailyUsage records
        """
        logger.info(f"Extracting data for {month_str}")

        if not self._select_month(month_str):
            logger.warning(f"Could not select month: {month_str}")
            return []

        # Wait for data to load and API to respond
        time.sleep(3)

        # Extract from network logs (don't clear - we want all data)
        raw_data = self._extract_from_logs()

        # If no data from logs, try page-based extraction
        if not raw_data:
            logger.info("Trying direct API extraction...")
            raw_data = self._extract_via_direct_api(month_str)

        if not raw_data:
            logger.warning(f"No data extracted for {month_str}")
            return []

        records = self._parse_daily_data(raw_data, month_str)
        logger.info(f"Extracted {len(records)} records for {month_str}")

        return records

    def _extract_via_direct_api(self, month_str: str) -> list[dict[str, Any]]:
        """Extract data by executing JavaScript to fetch from API directly."""
        try:
            # Find meter number from page
            meter_script = """
            var selects = document.querySelectorAll('select');
            for (var s of selects) {
                for (var o of s.options) {
                    if (/^\\d{9}$/.test(o.value)) return o.value;
                }
            }
            return null;
            """
            meter = self.driver.execute_script(meter_script)

            if not meter:
                logger.warning("Could not find meter number")
                return []

            logger.info(f"Found meter: {meter}")

            # Execute fetch directly
            fetch_script = f"""
            return fetch('/ajax/waterMeter/getSmartWaterMeterConsumptions?meter={meter}&startDate=&endDate=&graphType=MonthlyByDays&duration=Last30Days')
                .then(r => r.json())
                .catch(e => null);
            """

            data = self.driver.execute_script(fetch_script)

            if data and data.get("Lines"):
                logger.info(f"Direct API returned {len(data['Lines'])} records")
                return [data]

            return []

        except Exception as e:
            logger.warning(f"Direct API extraction failed: {e}")
            return []

    def extract_all_months(
        self,
        months: list[str] | None = None,
    ) -> list[DailyUsage]:
        """
        Extract daily usage data for multiple months.

        Args:
            months: List of month strings (defaults to last 12 months)

        Returns:
            List of DailyUsage records (deduplicated)
        """
        if months is None:
            # Generate last 12 months
            months = []
            today = datetime.now()
            for i in range(12):
                date = today - timedelta(days=30 * i)
                month_str = date.strftime("%b-%Y")
                months.append(month_str)

        all_records = []
        seen_dates = set()

        for month in months:
            records = self.extract_month(month)
            for record in records:
                if record.date not in seen_dates:
                    seen_dates.add(record.date)
                    all_records.append(record)

        # Sort by date
        all_records.sort(key=lambda x: x.date)
        logger.info(f"Total unique records: {len(all_records)}")

        return all_records

    def extract_available_hourly(self) -> list[HourlyUsage]:
        """
        Extract hourly usage data for all available dates.

        Thames Water has a 3-day delay and provides 7 days of hourly data.
        For example, on Dec 18, hourly data is available for Dec 9-15.

        Returns:
            List of HourlyUsage records
        """
        # Thames Water has ~3 day delay for data availability
        # And provides 7 days of hourly data
        data_delay_days = 3
        hourly_days_available = 7

        today = datetime.now()
        latest_available = today - timedelta(days=data_delay_days)
        earliest_available = latest_available - timedelta(days=hourly_days_available - 1)

        logger.info(
            f"Extracting hourly data for {earliest_available.strftime('%Y-%m-%d')} "
            f"to {latest_available.strftime('%Y-%m-%d')}"
        )

        # Use UI-based approach: select hourly view and capture from network logs
        all_hourly_records = self._extract_hourly_via_ui(
            earliest_available.strftime("%Y-%m-%d"),
            latest_available.strftime("%Y-%m-%d")
        )

        logger.info(f"Total hourly records extracted: {len(all_hourly_records)}")
        return all_hourly_records

    def _extract_hourly_via_ui(
        self, start_date: str, end_date: str
    ) -> list[HourlyUsage]:
        """
        Extract hourly data by selecting the hourly view and iterating through date options.

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format

        Returns:
            List of HourlyUsage records
        """
        logger.info(f"Extracting hourly data via UI selection ({start_date} to {end_date})")

        all_hourly_records = []

        try:
            # First, find and select the hourly view type
            selects = self.driver.find_elements(By.TAG_NAME, "select")

            hourly_view_selected = False
            date_dropdown = None

            for sel in selects:
                try:
                    select_obj = Select(sel)
                    options = [o.text for o in select_obj.options]
                    logger.debug(f"Select options: {options}")

                    # Look for hourly/daily view options
                    hourly_options = [
                        "Daily (by hours)",
                        "Hourly",
                        "By hour",
                        "24 hours",
                    ]

                    for opt in hourly_options:
                        if opt in options:
                            select_obj.select_by_visible_text(opt)
                            logger.info(f"Selected hourly view: {opt}")
                            hourly_view_selected = True
                            time.sleep(4)
                            break

                    if hourly_view_selected:
                        break
                except Exception:
                    continue

            if not hourly_view_selected:
                logger.warning("Could not find hourly view option in dropdowns")
                return []

            # Wait for hourly view to load
            time.sleep(3)

            # Now look for a date selection dropdown that appeared
            selects = self.driver.find_elements(By.TAG_NAME, "select")
            logger.info(f"Found {len(selects)} dropdowns after selecting hourly view")

            for sel in selects:
                try:
                    select_obj = Select(sel)
                    options = [o.text for o in select_obj.options]
                    logger.debug(f"Checking dropdown with options: {options[:5]}...")

                    # Look for dropdown with date-like options
                    # Format could be: "15-December", "14-Dec", or "15-12-2025" (DD-MM-YYYY)
                    date_options = []
                    for opt in options:
                        # Check if option looks like a date with month name
                        if any(month in opt for month in [
                            "January", "February", "March", "April", "May", "June",
                            "July", "August", "September", "October", "November", "December",
                            "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
                        ]):
                            date_options.append(opt)
                        # Check for DD-MM-YYYY format (e.g., "15-12-2025")
                        elif re.match(r'^\d{1,2}-\d{1,2}-\d{4}$', opt):
                            date_options.append(opt)

                    if date_options:
                        logger.info(f"Found date dropdown with {len(date_options)} date options: {date_options[:3]}...")
                        date_dropdown = select_obj

                        # Iterate through each date option
                        for date_opt in date_options:
                            try:
                                select_obj.select_by_visible_text(date_opt)
                                logger.info(f"Selected date: {date_opt}")
                                time.sleep(3)

                                # Extract from network logs
                                raw_data = self._extract_from_logs()

                                if raw_data:
                                    # Parse the date from the option
                                    record_date = self._parse_date_option(date_opt, end_date)
                                    records = self._parse_hourly_records(raw_data, record_date)
                                    if records:
                                        all_hourly_records.extend(records)
                                        logger.info(f"Got {len(records)} hourly records for {record_date}")
                            except Exception as e:
                                logger.debug(f"Could not select date {date_opt}: {e}")
                                continue

                        break

                except Exception as e:
                    logger.debug(f"Error checking dropdown: {e}")
                    continue

            # If no date dropdown found, extract whatever is currently displayed
            if not date_dropdown:
                logger.info("No date dropdown found, extracting current view")
                raw_data = self._extract_from_logs()

                if raw_data:
                    records = self._parse_hourly_records(raw_data, end_date)
                    all_hourly_records.extend(records)
                    logger.info(f"Got {len(records)} hourly records from current view")

            logger.info(f"Total hourly records extracted via UI: {len(all_hourly_records)}")
            return all_hourly_records

        except Exception as e:
            logger.error(f"Hourly extraction via UI failed: {e}")
            return []

    def _parse_date_option(self, date_opt: str, fallback_date: str) -> str:
        """
        Parse a date option string into YYYY-MM-DD format.

        Handles formats like:
        - "15-12-2025" (DD-MM-YYYY)
        - "15-December"
        - "December 15"

        Args:
            date_opt: Date option string from dropdown
            fallback_date: Fallback date if parsing fails

        Returns:
            Date in YYYY-MM-DD format
        """
        try:
            # Try DD-MM-YYYY format first (e.g., "15-12-2025")
            dd_mm_yyyy_match = re.match(r'^(\d{1,2})-(\d{1,2})-(\d{4})$', date_opt)
            if dd_mm_yyyy_match:
                day = int(dd_mm_yyyy_match.group(1))
                month = int(dd_mm_yyyy_match.group(2))
                year = int(dd_mm_yyyy_match.group(3))
                return f"{year}-{month:02d}-{day:02d}"

            # Common formats: "15-December", "15-Dec", "December 15", "Dec 15"
            month_map = {
                "January": 1, "February": 2, "March": 3, "April": 4,
                "May": 5, "June": 6, "July": 7, "August": 8,
                "September": 9, "October": 10, "November": 11, "December": 12,
                "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4,
                "Jun": 6, "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12
            }

            # Extract day and month
            day = None
            month = None

            # Try to find day number
            day_match = re.search(r'\b(\d{1,2})\b', date_opt)
            if day_match:
                day = int(day_match.group(1))

            # Try to find month
            for month_name, month_num in month_map.items():
                if month_name in date_opt:
                    month = month_num
                    break

            if day and month:
                # Determine year based on current date
                current_year = datetime.now().year
                current_month = datetime.now().month

                # If the month is greater than current month, it's probably last year
                if month > current_month:
                    year = current_year - 1
                else:
                    year = current_year

                return f"{year}-{month:02d}-{day:02d}"

        except Exception as e:
            logger.debug(f"Could not parse date option '{date_opt}': {e}")

        return fallback_date

    def _click_day_for_hourly(self, target_date: datetime) -> bool:
        """
        Try to click on a specific day in the chart to get hourly breakdown.

        Args:
            target_date: Date to click on

        Returns:
            True if successfully clicked, False otherwise
        """
        try:
            # Format the date as it might appear on the chart
            day = target_date.day
            month_name = target_date.strftime("%B")
            short_month = target_date.strftime("%b")

            # Try multiple selectors for clicking on a day
            click_selectors = [
                # SVG chart bars or points
                f"//text[contains(text(), '{day}-{month_name}')]",
                f"//text[contains(text(), '{day}-{short_month}')]",
                f"//*[contains(@data-date, '{target_date.strftime('%Y-%m-%d')}')]",
                # Chart elements
                f"//rect[contains(@aria-label, '{day}')]",
                f"//*[contains(@aria-label, '{target_date.strftime('%d %B')}')]",
            ]

            wait = WebDriverWait(self.driver, 5)

            for selector in click_selectors:
                try:
                    element = wait.until(EC.element_to_be_clickable((By.XPATH, selector)))
                    element.click()
                    logger.debug(f"Clicked element with selector: {selector}")
                    return True
                except TimeoutException:
                    continue
                except Exception as e:
                    logger.debug(f"Could not click with selector {selector}: {e}")
                    continue

            # Try clicking by executing JavaScript on chart elements
            click_script = f"""
            // Try to find and click chart elements for day {day}
            var elements = document.querySelectorAll('rect, path, circle, text');
            for (var el of elements) {{
                var text = el.textContent || el.getAttribute('aria-label') || '';
                if (text.includes('{day}-') || text.includes('{day} ')) {{
                    el.click();
                    return true;
                }}
            }}
            return false;
            """
            result = self.driver.execute_script(click_script)
            if result:
                logger.debug(f"Clicked via JavaScript for day {day}")
                return True

            return False

        except Exception as e:
            logger.debug(f"Failed to click day: {e}")
            return False

    def _extract_hourly_via_api(self, date_str: str) -> list[HourlyUsage]:
        """
        Extract hourly data via direct API call with proper date selection.

        Args:
            date_str: Date in YYYY-MM-DD format

        Returns:
            List of HourlyUsage records
        """
        try:
            # Find meter number
            meter_script = """
            var selects = document.querySelectorAll('select');
            for (var s of selects) {
                for (var o of s.options) {
                    if (/^\\d{9}$/.test(o.value)) return o.value;
                }
            }
            return null;
            """
            meter = self.driver.execute_script(meter_script)

            if not meter:
                return []

            # Format date for API
            target_date = datetime.strptime(date_str, "%Y-%m-%d")
            api_date = target_date.strftime("%d-%b-%Y")

            # Try API call
            fetch_script = f"""
            return fetch('/ajax/waterMeter/getSmartWaterMeterConsumptions?meter={meter}&startDate={api_date}&endDate={api_date}&graphType=DailyByHours')
                .then(r => r.json())
                .catch(e => null);
            """

            data = self.driver.execute_script(fetch_script)

            if data and data.get("Lines"):
                return self._parse_hourly_records([data], date_str)

            return []

        except Exception as e:
            logger.debug(f"API hourly extraction failed for {date_str}: {e}")
            return []

    def _parse_hourly_records(
        self, raw_data: list[dict], date_str: str
    ) -> list[HourlyUsage]:
        """
        Parse raw API data into HourlyUsage records.

        Args:
            raw_data: List of data sets from API
            date_str: Date for these records

        Returns:
            List of HourlyUsage records
        """
        hourly_records = []

        for data_set in raw_data:
            lines = data_set.get("Lines", [])

            # Only process if this looks like hourly data (24 or fewer records)
            if len(lines) > 24:
                continue

            for line in lines:
                hour_label = line.get("Label", "")

                try:
                    # Parse hour from label (e.g., "14:00", "2 PM", "02:00")
                    hour = None
                    if ":" in hour_label:
                        hour = int(hour_label.split(":")[0])
                    elif "AM" in hour_label.upper() or "PM" in hour_label.upper():
                        hour_str = hour_label.upper().replace("AM", "").replace("PM", "").strip()
                        hour = int(hour_str)
                        if "PM" in hour_label.upper() and hour != 12:
                            hour += 12
                        elif "AM" in hour_label.upper() and hour == 12:
                            hour = 0
                    else:
                        try:
                            hour = int(hour_label)
                        except ValueError:
                            continue

                    if hour is None or hour < 0 or hour > 23:
                        continue

                    # Capture meter reading if available
                    meter_reading = line.get("Read")

                    hourly_records.append(HourlyUsage(
                        date=date_str,
                        hour=hour,
                        usage_litres=float(line.get("Usage", 0)),
                        meter_reading=float(meter_reading) if meter_reading else None,
                        source="scraper",
                    ))

                except (ValueError, IndexError) as e:
                    logger.debug(f"Could not parse hourly record: {e}")
                    continue

        return hourly_records

    def _extract_hourly_for_date(self, date_str: str) -> list[HourlyUsage]:
        """
        Extract hourly data for a specific date using direct API call.

        Args:
            date_str: Date in YYYY-MM-DD format

        Returns:
            List of HourlyUsage records for that date
        """
        logger.info(f"Extracting hourly data for {date_str}")

        try:
            # Find meter number - try multiple methods
            meter_script = """
            // Method 1: Look in select options
            var selects = document.querySelectorAll('select');
            for (var s of selects) {
                for (var o of s.options) {
                    if (/^\\d{9}$/.test(o.value)) return o.value;
                }
            }
            // Method 2: Look in page text for meter pattern
            var text = document.body.innerText;
            var match = text.match(/\\b(\\d{9})\\b/);
            if (match) return match[1];
            // Method 3: Look in data attributes
            var elements = document.querySelectorAll('[data-meter], [data-meter-id]');
            for (var el of elements) {
                var val = el.getAttribute('data-meter') || el.getAttribute('data-meter-id');
                if (val && /^\\d{9}$/.test(val)) return val;
            }
            return null;
            """
            meter = self.driver.execute_script(meter_script)

            if not meter:
                logger.warning("Could not find meter number for hourly extraction")
                # Try to get meter from page source as fallback
                page_source = self.driver.page_source
                meter_match = re.search(r'\b(\d{9})\b', page_source)
                if meter_match:
                    meter = meter_match.group(1)
                    logger.info(f"Found meter from page source: {meter}")
                else:
                    return []

            logger.debug(f"Using meter: {meter} for hourly extraction")

            # Format date for API (e.g., "15-Dec-2025")
            target_date = datetime.strptime(date_str, "%Y-%m-%d")
            api_date = target_date.strftime("%d-%b-%Y")

            # Fetch hourly data via API - try different parameter combinations
            # First try with specific date
            fetch_script = f"""
            return fetch('/ajax/waterMeter/getSmartWaterMeterConsumptions?meter={meter}&startDate={api_date}&endDate={api_date}&graphType=DailyByHours&duration=')
                .then(r => r.json())
                .catch(e => ({{error: e.toString()}}));
            """

            data = self.driver.execute_script(fetch_script)
            logger.debug(f"API response for {date_str}: {data}")

            if not data or not data.get("Lines"):
                # Try with Yesterday duration
                fetch_script2 = f"""
                return fetch('/ajax/waterMeter/getSmartWaterMeterConsumptions?meter={meter}&graphType=DailyByHours&duration=Yesterday')
                    .then(r => r.json())
                    .catch(e => ({{error: e.toString()}}));
                """
                data = self.driver.execute_script(fetch_script2)
                logger.debug(f"API response (Yesterday): {data}")

            if not data or not data.get("Lines"):
                logger.debug(f"No hourly data for {date_str}")
                return []

            hourly_records = []
            for line in data.get("Lines", []):
                hour_label = line.get("Label", "")
                try:
                    # Parse hour from label (e.g., "14:00" or "2 PM")
                    if ":" in hour_label:
                        hour = int(hour_label.split(":")[0])
                    elif "AM" in hour_label.upper() or "PM" in hour_label.upper():
                        hour_str = hour_label.upper().replace("AM", "").replace("PM", "").strip()
                        hour = int(hour_str)
                        if "PM" in hour_label.upper() and hour != 12:
                            hour += 12
                        elif "AM" in hour_label.upper() and hour == 12:
                            hour = 0
                    else:
                        continue

                    hourly_records.append(HourlyUsage(
                        date=date_str,
                        hour=hour,
                        usage_litres=float(line.get("Usage", 0)),
                        source="scraper",
                    ))

                except (ValueError, IndexError) as e:
                    logger.debug(f"Could not parse hour label '{hour_label}': {e}")
                    continue

            logger.info(f"Extracted {len(hourly_records)} hourly records for {date_str}")
            return hourly_records

        except Exception as e:
            logger.warning(f"Hourly extraction failed for {date_str}: {e}")
            return []

    def extract_previous_day_hourly(self) -> list[HourlyUsage]:
        """
        Extract hourly usage data for available dates.

        Note: Thames Water has a 3-day delay, so "previous day" is actually
        the most recent available date (typically 3 days ago).

        Returns:
            List of HourlyUsage records
        """
        # Use the new method that handles correct date range
        return self.extract_available_hourly()

    def _extract_hourly_legacy(self) -> list[HourlyUsage]:
        """
        Legacy hourly extraction via UI selection (fallback method).

        Returns:
            List of HourlyUsage records
        """
        # Calculate correct date (3-day delay)
        target_date = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        logger.info(f"Legacy hourly extraction for {target_date}")

        # Try to find hourly view option
        try:
            selects = self.driver.find_elements(By.TAG_NAME, "select")

            for sel in selects:
                try:
                    select_obj = Select(sel)
                    options = [o.text for o in select_obj.options]

                    # Look for hourly/daily options
                    hourly_options = [
                        "Hourly", "Daily (by hours)", "By hour",
                        "Yesterday", "Last 24 hours"
                    ]

                    for opt in hourly_options:
                        if opt in options:
                            select_obj.select_by_visible_text(opt)
                            logger.info(f"Selected hourly view: {opt}")
                            time.sleep(4)
                            break
                except Exception:
                    continue

        except Exception as e:
            logger.warning(f"Could not find hourly view: {e}")

        # Extract from logs
        raw_data = self._extract_from_logs()

        hourly_records = []
        for data_set in raw_data:
            lines = data_set.get("Lines", [])
            for line in lines:
                hour_label = line.get("Label", "")
                try:
                    # Parse hour from label (e.g., "14:00" or "2 PM")
                    if ":" in hour_label:
                        hour = int(hour_label.split(":")[0])
                    elif "AM" in hour_label or "PM" in hour_label:
                        hour_str = hour_label.replace("AM", "").replace("PM", "").strip()
                        hour = int(hour_str)
                        if "PM" in hour_label and hour != 12:
                            hour += 12
                        elif "AM" in hour_label and hour == 12:
                            hour = 0
                    else:
                        continue

                    hourly_records.append(HourlyUsage(
                        date=target_date,
                        hour=hour,
                        usage_litres=float(line.get("Usage", 0)),
                        source="scraper",
                    ))

                except (ValueError, IndexError):
                    continue

        logger.info(f"Extracted {len(hourly_records)} hourly records")
        return hourly_records

    def _save_debug_screenshot(self, name: str) -> None:
        """Save debug screenshot."""
        try:
            if self.driver:
                path = f"logs/debug_{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                self.driver.save_screenshot(path)
                logger.debug(f"Saved debug screenshot: {path}")
        except Exception:
            pass

    def run(
        self,
        months: list[str] | None = None,
        include_hourly: bool = False,
    ) -> tuple[list[DailyUsage], list[HourlyUsage]]:
        """
        Main extraction method (for backfill/historical data).

        Args:
            months: Specific months to extract (defaults to last 12)
            include_hourly: Whether to also extract hourly data

        Returns:
            Tuple of (daily_records, hourly_records)
        """
        daily_records = []
        hourly_records = []

        try:
            self._setup_driver()

            if not self._login():
                raise LoginError("Login failed")

            self._navigate_to_water_use()
            self._set_daily_view()

            # Extract daily data
            daily_records = self.extract_all_months(months)

            # Optionally extract hourly data
            if include_hourly:
                hourly_records = self.extract_previous_day_hourly()

            return daily_records, hourly_records

        finally:
            self.close()

    def run_daily_sync(self) -> tuple[list[DailyUsage], list[HourlyUsage], str | None]:
        """
        Simplified daily sync - gets latest daily and hourly data.

        Workflow:
        1. Select "Monthly (by days)" + "Last 30 days" for daily data
        2. Select "Daily (by hours)" + latest date for hourly data

        Returns:
            Tuple of (daily_records, hourly_records, attempted_hourly_date)
            The attempted_hourly_date is useful for alerting when no data was found.
        """
        daily_records = []
        hourly_records = []
        attempted_hourly_date = None

        try:
            self._setup_driver()

            if not self._login():
                raise LoginError("Login failed")

            self._navigate_to_water_use()

            # Step 1: Get daily data from "Last 30 days"
            logger.info("Extracting daily data from 'Last 30 days'")
            daily_records = self._extract_last_30_days()

            # Step 2: Get hourly data for latest available date
            logger.info("Extracting hourly data for latest available date")
            hourly_records, attempted_hourly_date = self._extract_latest_hourly()

            return daily_records, hourly_records, attempted_hourly_date

        finally:
            self.close()

    def _select_view_option(self, option_text: str, timeout: int = 60) -> bool:
        """Poll for a <select> containing option_text and select it.

        The my-meters-usage SPA renders its dropdowns well after page load, so
        a fixed post-navigation sleep is not enough (empty syncs, 2026-09-03).
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            for sel in self.driver.find_elements(By.TAG_NAME, "select"):
                try:
                    select_obj = Select(sel)
                    if option_text in [o.text for o in select_obj.options]:
                        select_obj.select_by_visible_text(option_text)
                        logger.info(f"Selected '{option_text}' view")
                        time.sleep(3)
                        return True
                except Exception:
                    continue
            time.sleep(2)
        return False

    def _extract_last_30_days(self) -> list[DailyUsage]:
        """
        Extract daily data by selecting 'Monthly (by days)' and 'Last 30 days' dropdowns.

        Uses UI-based approach:
        1. Select "Monthly (by days)" from view dropdown
        2. Select "Last 30 days" from period dropdown
        3. Extract data from network logs
        4. Parse using _parse_daily_data()

        Returns:
            List of DailyUsage records
        """
        records = []

        try:
            # Step 1: Select "Monthly (by days)" view (polls while the SPA renders)
            if not self._select_view_option("Monthly (by days)"):
                logger.warning("Could not find 'Monthly (by days)' option")
                return records

            # Step 2: Select "Last 30 days" from period dropdown
            selects = self.driver.find_elements(By.TAG_NAME, "select")
            last_30_days_selected = False

            for sel in selects:
                try:
                    select_obj = Select(sel)
                    options = [o.text for o in select_obj.options]

                    if "Last 30 days" in options:
                        select_obj.select_by_visible_text("Last 30 days")
                        logger.info("Selected 'Last 30 days' period")
                        last_30_days_selected = True
                        time.sleep(4)  # Wait for data to load
                        break
                except Exception:
                    continue

            if not last_30_days_selected:
                logger.warning("Could not find 'Last 30 days' option")
                return records

            # Step 3: Extract data from network logs
            raw_data = self._extract_from_logs()

            if not raw_data:
                logger.warning("No data captured from network logs for daily extraction")
                return records

            # Step 4: Parse using _parse_daily_data with "Last 30 days" context
            records = self._parse_daily_data(raw_data, "Last 30 days")
            logger.info(f"Extracted {len(records)} daily records via UI selection")

        except Exception as e:
            logger.error(f"Error extracting last 30 days: {e}")

        return records

    def _get_meter_number(self) -> str | None:
        """Find meter number from page dropdown."""
        try:
            meter_script = """
            var selects = document.querySelectorAll('select');
            for (var s of selects) {
                for (var o of s.options) {
                    if (/^\\d{9}$/.test(o.value)) return o.value;
                }
            }
            return null;
            """
            meter = self.driver.execute_script(meter_script)
            if meter:
                logger.info(f"Found meter: {meter}")
            return meter
        except Exception as e:
            logger.warning(f"Error finding meter number: {e}")
            return None

    def _extract_latest_hourly(self) -> tuple[list[HourlyUsage], str | None]:
        """
        Extract hourly data for the latest available date using UI selection.

        Uses dropdown selection and network log capture instead of direct API
        (which may fail with permission errors).

        Returns:
            Tuple of (List of HourlyUsage records, attempted date string or None)
        """
        records = []
        attempted_date = None

        try:
            # Step 1: Select "Daily (by hours)" view (polls while the SPA renders)
            if not self._select_view_option("Daily (by hours)"):
                logger.warning("Could not find 'Daily (by hours)' option")
                return records, attempted_date

            # Step 2: Find the date dropdown and select the latest date
            selects = self.driver.find_elements(By.TAG_NAME, "select")
            latest_date_str = None
            date_str = None

            for sel in selects:
                try:
                    select_obj = Select(sel)
                    options = [o.text for o in select_obj.options]

                    # Look for dropdown with date-like options (DD-MM-YYYY format)
                    for opt in options:
                        if re.match(r'^\d{1,2}-\d{1,2}-\d{4}$', opt):
                            latest_date_str = opt  # First one is the latest
                            logger.info(f"Found latest date in dropdown: {latest_date_str}")

                            # Select this date to trigger data load
                            select_obj.select_by_visible_text(latest_date_str)
                            logger.info(f"Selected date: {latest_date_str}")
                            time.sleep(5)  # Wait for data to load
                            break

                    if latest_date_str:
                        break
                except Exception:
                    continue

            if not latest_date_str:
                logger.warning("Could not find date dropdown for hourly data")
                return records, attempted_date

            # Parse date from DD-MM-YYYY to YYYY-MM-DD for records
            match = re.match(r'^(\d{1,2})-(\d{1,2})-(\d{4})$', latest_date_str)
            if not match:
                logger.warning(f"Could not parse date: {latest_date_str}")
                return records, attempted_date

            day = int(match.group(1))
            month = int(match.group(2))
            year = int(match.group(3))
            date_str = f"{year}-{month:02d}-{day:02d}"
            attempted_date = date_str  # Track which date we attempted

            # Step 3: Extract data from network logs
            raw_data = self._extract_from_logs()

            if not raw_data:
                logger.warning(f"No data captured from network logs for {date_str}")
                return records, attempted_date

            # Step 4: Parse hourly data from network response
            # Find the response with hourly data (Labels like "0:00", "1:00", etc.)
            hourly_data = None
            for item in raw_data:
                if isinstance(item, dict) and "Lines" in item:
                    lines = item["Lines"]
                    if lines and len(lines) > 0:
                        first_label = str(lines[0].get("Label", ""))
                        # Check if this looks like hourly data (contains ":" or is a small number)
                        if ":" in first_label or (first_label.isdigit() and int(first_label) <= 23):
                            hourly_data = lines
                            break

            if not hourly_data:
                logger.warning(f"No hourly data found in network logs for {date_str}")
                return records, attempted_date

            logger.info(f"Found {len(hourly_data)} hourly records in network logs")

            # Parse hourly records
            for line in hourly_data:
                hour_label = str(line.get("Label", ""))
                usage_value = float(line.get("Usage", 0))
                meter_reading = line.get("Read")

                # Parse hour from label (e.g., "14:00", "2:00", "0:00" or just "14", "2", "0")
                hour = None
                try:
                    if ":" in hour_label:
                        hour = int(hour_label.split(":")[0])
                    elif hour_label.isdigit():
                        hour = int(hour_label)
                except (ValueError, IndexError):
                    logger.debug(f"Could not parse hour from label: {hour_label}")
                    continue

                if hour is not None and 0 <= hour <= 23:
                    record = HourlyUsage(
                        date=date_str,
                        hour=hour,
                        usage_litres=usage_value,
                        meter_reading=float(meter_reading) if meter_reading else None,
                        source="scraper",
                    )
                    records.append(record)

            logger.info(f"Parsed {len(records)} hourly records for {date_str}")

        except Exception as e:
            logger.error(f"Error extracting latest hourly: {e}")

        return records, attempted_date

    def _parse_daily_record(self, item: dict) -> DailyUsage | None:
        """Parse a daily record from API response item."""
        try:
            date_str = item.get("readingDate", item.get("date", ""))
            if date_str:
                # Handle various date formats
                if "T" in date_str:
                    date_str = date_str.split("T")[0]

                usage = float(item.get("consumption", item.get("usage", 0)))
                meter_reading = item.get("meterReading", item.get("cumulativeConsumption"))

                if usage > 0:
                    return DailyUsage(
                        date=date_str,
                        usage_litres=usage,
                        meter_reading=float(meter_reading) if meter_reading else None,
                        is_estimated=item.get("isEstimated", False),
                        source="scraper",
                    )
        except Exception as e:
            logger.debug(f"Error parsing daily record: {e}")
        return None

    def _parse_hourly_record(self, item: dict, date_str: str) -> HourlyUsage | None:
        """Parse an hourly record from API response item."""
        try:
            hour = item.get("hour", item.get("readingHour"))
            usage = float(item.get("consumption", item.get("usage", 0)))

            if hour is not None:
                return HourlyUsage(
                    date=date_str,
                    hour=int(hour),
                    usage_litres=usage,
                )
        except Exception as e:
            logger.debug(f"Error parsing hourly record: {e}")
        return None

    def _extract_daily_from_ui(self) -> list[DailyUsage]:
        """Fallback: Extract daily data from chart UI elements."""
        records = []
        try:
            # Look for chart data in the page
            chart_elements = self.driver.find_elements(By.CSS_SELECTOR, "[data-usage], .bar, .chart-bar")
            logger.info(f"Found {len(chart_elements)} potential chart elements")
            # UI extraction would need to be implemented based on actual page structure
        except Exception as e:
            logger.debug(f"UI extraction failed: {e}")
        return records

    def _extract_hourly_from_ui(self, date_str: str) -> list[HourlyUsage]:
        """Fallback: Extract hourly data from chart UI elements."""
        records = []
        try:
            # Look for chart data in the page
            chart_elements = self.driver.find_elements(By.CSS_SELECTOR, "[data-usage], .bar, .chart-bar")
            logger.info(f"Found {len(chart_elements)} potential chart elements for hourly")
            # UI extraction would need to be implemented based on actual page structure
        except Exception as e:
            logger.debug(f"Hourly UI extraction failed: {e}")
        return records

    def close(self) -> None:
        """Close the browser."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def __enter__(self):
        """Context manager entry."""
        self._setup_driver()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
        return False
