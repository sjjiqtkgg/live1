# custom_output.py
from base.output import RoomCSVOutput

class CallbackOutput(RoomCSVOutput):
    def __init__(self, room_id, callback):
        super().__init__(room_id)
        self.callback = callback

    def output(self, msg):
        """重写输出方法：将消息通过回调函数转发到前端"""
        if self.callback:
            try:
                self.callback(msg)
            except Exception as e:
                print(f"[CallbackOutput] 回调时出错: {e}")
        # 不调用父类的写入文件功能，避免在 Render 上积累大量文件
