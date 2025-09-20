from nicegui import ui


@ui.page("/")
async def main_page():
    async def get_viewport_size():
        width = await ui.run_javascript("return window.innerWidth")
        height = await ui.run_javascript("return window.innerHeight")
        ui.notify(f"视口宽度：{width}px，视口高度：{height}px")

    ui.button("获取视口尺寸", on_click=get_viewport_size)


ui.run()
