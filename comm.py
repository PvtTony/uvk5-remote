import asyncio
import serial_asyncio
import serial
import logging
from enum import IntEnum
from typing import Optional, List, Union
import numpy as np
from PIL import Image, ImageDraw
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
SCREEN_DATA_LEN = 8192

# 设置事件循环
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# 定义像素大小
PIXEL_WIDTH = 5
PIXEL_HEIGHT = 7

class Packet:
    """命令包定义"""
    Hello = 0x514
    GetRssi = 0x527
    KeyPress = 0x801
    GetScreen = 0x803
    Scan = 0x808
    ScanAdjust = 0x809
    ScanReply = 0x908
    WriteRegisters = 0x850
    ReadRegisters = 0x851
    RegisterInfo = 0x951
    WriteEeprom = 0x51D
    ReadEeprom = 0x51B


class Stage(IntEnum):
    """数据包解析状态"""
    Idle = 0  # 空闲状态，等待新数据包的开始标记(0xAB 或 0xB5)
    CD = 1  # 等待第二个包头字节(0xCD)，确认是标准数据包
    LenLSB = 2  # 接收数据包长度的低字节
    LenMSB = 3  # 接收数据包长度的高字节
    Data = 4  # 接收加密的数据内容
    CrcLSB = 5  # 接收CRC校验和的低字节
    CrcMSB = 6  # 接收CRC校验和的高字节
    DC = 7  # 等待第一个包尾字节(0xDC)
    BA = 8  # 等待第二个包尾字节(0xBA)
    UiType = 9  # 处理UI类型的数据包


class KeyCode(IntEnum):
    """对讲机按键代码
    
    特殊按键值：
    19 - STOP_KEY: 停止所有按键输入
    """
    FM = 0  # "0\nFM"
    BAND = 1  # "1\nBAND"
    AB = 2  # "2\nA/B"
    VFO_MR = 3  # "3\nVFO/MR"
    FC = 4  # "4\nFC"
    SL_SR = 5  # "5\nSL/SR"
    TX_PWR = 6  # "6\nTX PWR"
    VOX = 7  # "7\nVOX"
    R = 8  # "8\nR"
    CALL = 9  # "9\nCALL"
    M_A = 10  # "M  🅐"
    UP_B = 11  # "↑  🅑"
    DOWN_C = 12  # "↓  🅒"
    EXIT_D = 13  # "EXIT 🅓"
    SCAN = 14  # "*\nSCAN"
    F_LOCK = 15  # "F\n  # 🔒"
    PTT = 16  # "PTT"
    USER2 = 17  # 自定义按键2 Custom2
    USER1 = 18  # 自定义按键1 Custom1
    STOP_KEY = 19  # 停止所有按键输入


class QuanshengProtocol(asyncio.Protocol):
    def __init__(self, comm):
        self.comm = comm
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        logger.info("Connection established")
        self.comm.connection_ready.set()

    def data_received(self, data):
        # logger.info(f"RECV={data}")
        for byte in data:
            self.comm.process_byte(byte)

    def connection_lost(self, exc):
        logger.info("Connection lost")
        self.transport = None


class QuanshengComm:
    def __init__(self, port: str = "COM1", baudrate: int = 38400):
        self.port = port
        self.baudrate = baudrate
        self.transport = None
        self.protocol = None
        self.stage = Stage.Idle
        self.data = bytearray()
        self.p_len = 0
        self.p_cnt = 0
        self.is_running = True
        self.screen_data: np.ndarray = np.zeros((64, 128), dtype=np.uint8)
        self.on_screen_update = None  # 屏幕更新回调函数
        self.connection_ready = asyncio.Event()

        # 加密用的XOR数组
        self.xor_array = bytes([
            0x16, 0x6c, 0x14, 0xe6, 0x2e, 0x91, 0x0d, 0x40,
            0x21, 0x35, 0xd5, 0x40, 0x13, 0x03, 0xe9, 0x80
        ])
        logger.info(f"QuanshengComm init port={port} baudrate={baudrate}")

    async def connect(self):
        """异步连接串口"""
        try:
            loop = asyncio.get_event_loop()
            self.transport, self.protocol = await serial_asyncio.create_serial_connection(
                loop,
                lambda: QuanshengProtocol(self),
                self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE
            )
            return True
        except Exception as e:
            logger.error(f"Error connecting to port: {e}")
            return False

    def crypt(self, byte: int, xor_index: int) -> int:
        """加密/解密单个字节"""
        return byte ^ self.xor_array[xor_index & 15]

    def crc16(self, byte: int, crc: int) -> int:
        """计算CRC16"""
        crc ^= byte << 8
        for _ in range(8):
            crc <<= 1
            if crc > 0xffff:
                crc ^= 0x1021
                crc &= 0xffff
        return crc

    async def send_command(self, cmd: int, *args: Union[int, bytes, List[int]]):
        """异步发送命令"""
        if not self.transport:
            logger.error("No connection available")
            return

        # 构建数据包
        data = bytearray([0xAB, 0xCD, 0x00, 0x00])
        data.extend([cmd & 0xFF, (cmd >> 8) & 0xFF])

        # 处理参数
        params = bytearray()
        for arg in args:
            if isinstance(arg, int):
                if arg <= 0xFF:
                    params.append(arg)
                elif arg <= 0xFFFF:
                    params.extend([arg & 0xFF, (arg >> 8) & 0xFF])
                else:
                    params.extend([
                        arg & 0xFF,
                        (arg >> 8) & 0xFF,
                        (arg >> 16) & 0xFF,
                        (arg >> 24) & 0xFF
                    ])
            elif isinstance(arg, (bytes, bytearray)):
                params.extend(arg)
            elif isinstance(arg, list):
                for val in arg:
                    params.append(val & 0xFF)

        # 添加参数长度
        param_len = len(params)
        data[6:8] = [param_len & 0xFF, (param_len >> 8) & 0xFF]
        data.extend(params)

        # 计算CRC和加密
        crc = 0
        for i in range(4, len(data)):
            crc = self.crc16(data[i], crc)
            data[i] = self.crypt(data[i], i - 4)

        # 添加加密后的CRC
        crc_bytes = [crc & 0xFF, (crc >> 8) & 0xFF]
        data.extend([
            self.crypt(crc_bytes[0], len(data) - 4),
            self.crypt(crc_bytes[1], len(data) - 3)
        ])

        # 添加包尾
        data.extend([0xDC, 0xBA])

        # 设置总长度
        total_len = len(data) - 8
        data[2:4] = [total_len & 0xFF, (total_len >> 8) & 0xFF]

        # 异步发送数据
        self.transport.write(data)
        logger.debug(f"SendCommand data={data}")
        await asyncio.sleep(0.1)

    def process_byte(self, byte: int):
        """处理接收到的字节"""
        if self.stage == Stage.Idle:
            if byte == 0xAB:
                self.stage = Stage.CD
            elif byte == 0xB5:
                self.stage = Stage.UiType

        elif self.stage == Stage.CD:
            self.stage = Stage.LenLSB if byte == 0xCD else Stage.Idle

        elif self.stage == Stage.LenLSB:
            self.p_len = byte
            self.stage = Stage.LenMSB

        elif self.stage == Stage.LenMSB:
            self.p_len |= byte << 8
            self.data = bytearray()
            self.p_cnt = 0
            self.stage = Stage.Data

        elif self.stage == Stage.Data:
            self.data.append(self.crypt(byte, self.p_cnt))
            self.p_cnt += 1
            if self.p_cnt >= self.p_len:
                self.stage = Stage.CrcLSB

        elif self.stage == Stage.CrcLSB:
            self.stage = Stage.CrcMSB

        elif self.stage == Stage.CrcMSB:
            self.stage = Stage.DC

        elif self.stage == Stage.DC:
            self.stage = Stage.BA if byte == 0xDC else Stage.Idle

        elif self.stage == Stage.BA:
            self.stage = Stage.Idle
            if byte == 0xBA:
                self.parse_packet(self.data)

    def parse_packet(self, data: bytearray):
        """解析数据包"""
        if len(data) < 2:
            return
        cmd = data[0] | (data[1] << 8)
        if cmd == Packet.GetScreen:
            offset = data[2] | (data[3] << 8)
            diff = int(data[4])
            logger.debug(f"GetScreen offset={offset}, diff={diff}")
            self.parse_screen(data[5:], diff)
            if self.on_screen_update:
                self.on_screen_update()
        else:
            pass

    def parse_screen(self, data: bytearray, diff) -> np.ndarray:
        """
        解析屏幕数据为 128x64 的显示矩阵
        
        Args:
            data: 24bit 为一组的字节数组，大小必须是 3 的整数倍
            diff: 是否为差分数据
            
        Returns:
            np.ndarray: 128x64 的布尔矩阵，1 表示像素点亮，0 表示不亮
        """

        if len(data) % 3 != 0:
            raise ValueError("数据长度必须是 3 的整数倍")

        # 创建 128x64 的零矩阵
        screen = np.zeros((64, 128), dtype=np.uint8) if diff == 0 else self.screen_data

        # 每次处理 3 个字节
        for i in range(0, len(data), 3):
            # 获取三个字节
            pixels = data[i]  # 8 个像素点的状态（从下到上）
            col = data[i + 1]  # 列号 (0-127)
            row = (data[i + 2] * 8)  # 起始行号

            # 检查坐标是否有效
            if col >= 128 or row >= 64:
                continue

            # 处理 8 个像素点
            for bit in range(8):
                pixel_row = row + bit  # 实际的行号
                if pixel_row >= 64:  # 确保不超出边界
                    break

                # 从最低位开始，获取每一位的值
                is_lit = (pixels >> bit) & 1
                screen[pixel_row, col] = is_lit
        self.screen_data = screen
        return screen

    async def close(self):
        """异步关闭连接"""
        if self.transport:
            try:
                # 发送停止命令
                # await self.send_command(Packet.KeyPress, 19)
                await asyncio.sleep(0.1)

                self.transport.close()
                self.transport = None
            except Exception as e:
                logger.error(f"Error closing connection: {e}")

    def export_screen(self, filename: str = "screen.png") -> bool:
        """将 screen_data (ndarray) 保存为图像文件

        Args:
            filename: 要保存的图像文件路径，默认为 "screen.png"

        Returns:
            bool: 保存成功返回 True，失败返回 False
        """
        try:
            if self.screen_data is None:
                logger.error("No screen data available")
                return False



            # 计算新的图像尺寸
            width = 128 * PIXEL_WIDTH
            height = 64 * PIXEL_HEIGHT

            # 创建新图像
            image = Image.new("RGB", (width, height), "white")
            draw = ImageDraw.Draw(image)

            # 绘制每个像素
            for y in range(64):
                for x in range(128):
                    if self.screen_data[y, x]:  # 使用 numpy 数组的索引方式
                        # 计算放大后的矩形坐标
                        x1 = x * PIXEL_WIDTH
                        y1 = y * PIXEL_HEIGHT
                        x2 = x1 + PIXEL_WIDTH - 1
                        y2 = y1 + PIXEL_HEIGHT - 1
                        # 绘制黑色矩形
                        draw.rectangle([x1, y1, x2, y2], fill="black")

            # 保存图像
            image.save(filename)
            logger.info(f"Screen image saved to {filename}")
            return True

        except Exception as e:
            logger.error(f"Error exporting screen: {e}")
            return False


# 使用示例
async def main():
    radio = QuanshengComm("COM6")
    try:
        if await radio.connect():
            logger.info("Connection is ready for communication")

            # 发送Hello命令
            await radio.send_command(Packet.Hello, 0x12345678)
            await asyncio.sleep(5)

            await radio.send_command(Packet.KeyPress, KeyCode.FM.value)
            await asyncio.sleep(0.5)
            await radio.send_command(Packet.KeyPress, KeyCode.STOP_KEY.value)

            # 获取屏幕内容
            await radio.send_command(Packet.GetScreen, 0)

            # 等待一段时间以接收数据
            await asyncio.sleep(15)

            # 关闭连接
            await radio.close()
    except Exception as e:
        logger.error(f"Error in main: {e}")
        await radio.close()


if __name__ == "__main__":
    asyncio.run(main())
