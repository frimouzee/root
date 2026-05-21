from contextlib import asynccontextmanager
from http.cookiejar import MozillaCookieJar
from typing import AsyncGenerator, Literal, Optional
from secrets import token_urlsafe
from pydantic import BaseModel
from anyio import CapacityLimiter
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from opentelemetry import trace
from utils.logger import log
import config

jar = MozillaCookieJar()
try:
    jar.load("cookies.txt")
except FileNotFoundError:
    log.warning("No cookies.txt found, creating empty cookie jar")
    jar.save("cookies.txt")

class CookieModel(BaseModel):
    name: str
    value: str
    url: Optional[str] = None
    domain: Optional[str] = None
    path: Optional[str] = None
    expires: int = -1
    httpOnly: Optional[bool] = False
    secure: Optional[bool] = False
    sameSite: Optional[Literal["Lax", "None", "Strict"]] = "Strict"

    class Config:
        from_attributes = True

class BrowserHandler:
    def __init__(self) -> None:
        self.limiter = CapacityLimiter(4)
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.tracer = trace.get_tracer(__name__)

    async def cleanup(self) -> None:
        """Cleanup browser resources."""
        with self.tracer.start_as_current_span("browser_cleanup"):
            if self.playwright:
                await self.playwright.stop()
            if self.browser:
                await self.browser.close()
            log.info("Browser resources cleaned up")

    async def init(self) -> None:
        """Initialize browser and context."""
        with self.tracer.start_as_current_span("browser_init"):
            try:
                await self.cleanup()
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(
                    proxy={
                        "server": f"http://{config.PROXY.HOST}:{config.PROXY.PORT}"
                    } if hasattr(config, 'PROXY') else None
                )
                
                self.context = await self.browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/93.0.4577.63 Safari/537.36"
                    )
                )
                
                cookies = [
                    cookie.dict(exclude_unset=True)
                    for _cookie in jar
                    if (cookie := CookieModel.from_orm(_cookie))
                ]
                if cookies:
                    await self.context.add_cookies(cookies)
                
                log.info("Browser initialized successfully")
                
            except Exception as e:
                log.error(f"Failed to initialize browser: {e}")
                raise

    @asynccontextmanager
    async def borrow_page(self) -> AsyncGenerator[Page, None]:
        """Borrow a page from the browser context."""
        if not self.context:
            raise RuntimeError("Browser context is not initialized")

        with self.tracer.start_as_current_span("borrow_page") as span:
            await self.limiter.acquire()
            identifier = token_urlsafe(12)
            span.set_attribute("page.id", identifier)
            
            try:
                page = await self.context.new_page()
                log.debug(f"Borrowed page ID {identifier}")
                yield page
            finally:
                self.limiter.release()
                await page.close()
                log.debug(f"Released page ID {identifier}") 