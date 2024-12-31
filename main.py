import sys
import asyncio
from PyQt6 import QtWidgets, uic
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QMessageBox, QInputDialog
from PyQt6.QtGui import QPixmap, QImage, QColor
import serial.tools.list_ports
from comm import QuanshengComm, KeyCode, Packet
import qasync
import logging

logger = logging.getLogger(__name__)


class RadioWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.button_map = {}
        uic.loadUi('radio.ui', self)
        
        # 获取可用COM口
        ports = [port.device for port in serial.tools.list_ports.comports()]
        if not ports:
            QMessageBox.critical(self, "错误", "未找到可用的COM口")
            sys.exit(1)
            
        # 让用户选择COM口
        port, ok = QInputDialog.getItem(self, "选择COM口", 
                                      "请选择要使用的COM口:", ports, 0, False)
        if not ok:
            sys.exit(0)
            
        # 初始化通信
        self.comm = QuanshengComm(port)
        
        # 设置屏幕更新回调
        self.comm.on_screen_update = self.refresh_screen
        
        # 设置按钮事件
        self.setup_buttons()
        
        # 使用 qasync 获取事件循环
        self.loop = asyncio.get_event_loop()
        # 运行初始化
        self.loop.create_task(self.init_comm())
        
    async def init_comm(self):
        """初始化通信连接"""
        logger.info("初始化通信连接")
        if await self.comm.connect():
            await asyncio.sleep(1)  # 确保连接稳定
            await self.comm.send_command(Packet.GetScreen, 0)
        else:
            QMessageBox.critical(self, "错误", "无法连接到对讲机")
            sys.exit(1)
        
    def setup_buttons(self):
        # 设置按键映射
        self.button_map = {
            self.btnMenu: KeyCode.M_A,
            self.btnUp: KeyCode.UP_B,
            self.btnDown: KeyCode.DOWN_C,
            self.btnExit: KeyCode.EXIT_D,
            self.btnPTT: KeyCode.PTT,
            self.btnCustom1: KeyCode.USER1,
            self.btnCustom2: KeyCode.USER2,
            self.btn1: KeyCode.BAND,
            self.btn2: KeyCode.AB,
            self.btn3: KeyCode.VFO_MR,
            self.btn4: KeyCode.FC,
            self.btn5: KeyCode.SL_SR,
            self.btn6: KeyCode.TX_PWR,
            self.btn7: KeyCode.VOX,
            self.btn8: KeyCode.R,
            self.btn9: KeyCode.CALL,
            self.btn0: KeyCode.FM,
            self.btnStar: KeyCode.SCAN,
            self.btnHash: KeyCode.F_LOCK,
        }
        
        # 修改按钮事件绑定方式
        for button, key_code in self.button_map.items():
            button.pressed.connect(self.create_press_handler(key_code))
            button.released.connect(self.create_release_handler(key_code))
            
        self.btnRefresh.clicked.connect(lambda: self.loop.create_task(self.refresh_command()))
        
    def create_press_handler(self, key_code):
        return lambda: self.loop.create_task(self.button_pressed(key_code))
    
    def create_release_handler(self, key_code):
        return lambda: self.loop.create_task(self.button_released(key_code))
        
    async def refresh_command(self):
        """刷新屏幕命令"""
        try:
            await asyncio.wait_for(self.comm.send_command(Packet.GetScreen, 0), timeout=1)
            await asyncio.sleep(0.5)  # 减少延迟时间
        except asyncio.TimeoutError:
            logger.warning("屏幕刷新超时")

    async def button_pressed(self, key_code):
        """按键按下事件"""
        await self.comm.send_command(Packet.KeyPress, key_code)
        await asyncio.sleep(0.5)
        await self.loop.create_task(self.comm.send_command(Packet.GetScreen, 1))


    async def button_released(self, key_code):
        """按键释放事件"""
        # await self.comm.send_command(Packet.KeyPress, key_code)
        # await asyncio.sleep(0.1)
        await self.comm.send_command(Packet.KeyPress, KeyCode.STOP_KEY)
        await asyncio.sleep(0.1)
        await self.loop.create_task(self.comm.send_command(Packet.GetScreen, 1))


    def refresh_screen(self):
        """刷新屏幕显示"""
        if self.comm.screen_data is None:
            return
            
        try:
            # 定义像素大小
            PIXEL_WIDTH = 5
            PIXEL_HEIGHT = 7
            
            # 计算新的图像尺寸 (注意 screen_data 现在是 64x128 的 ndarray)
            width = 128 * PIXEL_WIDTH
            height = 64 * PIXEL_HEIGHT
            image = QImage(width, height, QImage.Format.Format_RGB888)
            
            # 填充白色背景
            image.fill(QColor(255, 170, 0))
            
            # 绘制放大后的像素
            for y in range(64):
                for x in range(128):
                    if self.comm.screen_data[y, x]:  # 使用 numpy 数组的索引方式
                        # 如果点是黑色，填充对应的矩形区域
                        for py in range(PIXEL_HEIGHT):
                            for px in range(PIXEL_WIDTH):
                                image.setPixelColor(
                                    x * PIXEL_WIDTH + px,
                                    y * PIXEL_HEIGHT + py,
                                    Qt.GlobalColor.black
                                )
            
            # 使用 QTimer 延迟更新 UI，避免阻塞主线程
            QTimer.singleShot(0, lambda: self.update_ui(image))
        except Exception as e:
            logger.error(f"屏幕刷新错误: {e}")

    def update_ui(self, image):
        """更新 UI"""
        if self.screenView.scene() is None:
            self.screenView.setScene(QtWidgets.QGraphicsScene())
        self.screenView.scene().clear()
        self.screenView.scene().addPixmap(QPixmap.fromImage(image))

    def closeEvent(self, event):
        """窗口关闭事件"""
        event.ignore()
        async def do_close():
            await self.comm.close()
            self.deleteLater()
            # loop = asyncio.get_event_loop()
            # loop.close()
        
        self.loop.create_task(do_close())

if __name__ == '__main__':
    try:
        app = QtWidgets.QApplication(sys.argv)
        loop = qasync.QEventLoop(app)
        asyncio.set_event_loop(loop)

        window = RadioWindow()
        window.show()
        loop.run_forever()
    except KeyboardInterrupt:
        pass 