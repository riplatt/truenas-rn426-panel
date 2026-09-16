# SSD1305 init (33 bytes). NOTE: contains no display-on; 0xAF is sent after.
INIT_SEQ = bytes.fromhex("aed571a81fd9222002a1c8da12d80081cfb0d300210483220003100040a6a4db18")

# --------------------------------------------------------------------------
# LCD: SSD1305 driving logic, backend-agnostic. Takes a Gpio and drives it;
# geometry (pages, columns) and the init byte sequence come from MODELS.
# --------------------------------------------------------------------------
class LCD:
    def __init__(self, gpio, geometry, init_seq):
        self.gpio = gpio
        self.pages, self.cols = geometry
        self.init_seq = init_seq
        from PIL import ImageFont
        self.f1 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 16)
        self.f2 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 13)

    def _spi(self, byte, dc):
        self.gpio.set("CS", 0); self.gpio.set("DC", dc)
        for k in range(7, -1, -1):                       # MSB first
            self.gpio.set("CLK", 0)
            self.gpio.set("MOSI", (byte >> k) & 1)
            self.gpio.set("CLK", 1)                       # latch on rising edge
        self.gpio.set("DC", 1); self.gpio.set("CS", 1)

    def cmd(self, b): self._spi(b, 0)
    def dat(self, b): self._spi(b, 1)

    def init(self):
        self.gpio.idle()
        if self.gpio.en_can_pulse:
            self.gpio.set("EN", 0)
        # IMPORTANT: do NOT pulse RST. Pin 31 (RST) is a *shared front-board reset*
        # that also resets the MSP430 into a non-button-reporting mode, which only a
        # full power-cycle recovers. The SSD1305 is already powered (the BIOS used
        # it), so the command sequence alone re-inits it -- we just hold RST high.
        for b in self.init_seq:
            self.cmd(b)
        self.gpio.set("EN", 1)
        self.cmd(0xAF)                                   # display ON

    def sleep(self):
        # Pixels OFF only. Do NOT drive EN (pin 17) low: like RST, pin 17 is a
        # shared front-board line and holding it low resets the MSP430 out of
        # button-reporting mode (recoverable only by a power cycle). 0xAE blanks
        # the pixels (kills burn-in); the backlight stays on so the MCU is safe.
        self.cmd(0xAE)

    def wake(self):
        self.cmd(0xAF)                                   # pixels ON (EN was never lowered, no re-init needed)

    def show(self, img):
        px = img.load()
        for page in range(self.pages):                   # pages x 8 rows tall
            self.cmd(0xB0 | page); self.cmd(0x00); self.cmd(0x10)
            for c in range(self.cols):
                byte = 0
                for r in range(8):
                    if px[c, page * 8 + r]:
                        byte |= (1 << r)
                self.dat(byte)

    def lines(self, l1, l2):
        from PIL import Image, ImageDraw
        img = Image.new("1", (self.cols, self.pages * 8), 0); d = ImageDraw.Draw(img)
        d.text((4, -2), l1, font=self.f1, fill=1)
        d.text((4, 17), l2, font=self.f2, fill=1)
        self.show(img)
