from base.output import RoomCSVOutput

class CallbackOutput(RoomCSVOutput):
    """自定义输出器，将弹幕消息通过回调函数转发，而不是写入CSV文件"""
    def __init__(self, room_id, callback):
        super().__init__(room_id)
        self.callback = callback

    def output(self, msg):
        if self.callback:
            try:
                self.callback(msg)
            except Exception as e:
                print(f"[CallbackOutput] 回调出错: {e}")
