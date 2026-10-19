"""全屏图片预览及缩放、拖动交互。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import ui
from nicegui.events import GenericEventArguments


def build_contained_image_preview(image_url: str, on_close: Callable[[], None]):
    """在稳定的全屏视窗中创建保持比例的图片。"""
    with ui.element("div").classes("fixed inset-0 bg-transparent flex items-center justify-center p-3 sm:p-4"):
        with ui.element("div").classes(
            "relative w-[95vw] h-[90vh] max-w-[95vw] max-h-[90vh] overflow-hidden flex items-center justify-center"
        ):
            image = (
                ui.image(image_url)
                .props("fit=contain no-spinner")
                .classes("w-full h-full cursor-grab select-none")
                .style("transform-origin: center center;")
            )
        ui.button(icon="close", on_click=on_close).props("flat round color=grey-8").classes(
            "absolute top-3 right-3 z-10 bg-white/80 hover:bg-white shadow-sm"
        )
    return image


def show_interactive_image_preview(image_url: str) -> None:
    """打开支持滚轮缩放、左键拖动和中键复位的图片预览。"""
    state: dict[str, Any] = {
        "zoom": 1.0,
        "offset_x": 0.0,
        "offset_y": 0.0,
        "dragging": False,
        "last_x": 0.0,
        "last_y": 0.0,
    }
    preview = ui.dialog().props("maximized").classes("p-0")
    with preview:
        image = build_contained_image_preview(image_url, preview.close)

    def update_transform() -> None:
        image.style(
            "transform-origin: center center; "
            f"transform: translate({state['offset_x']}px, {state['offset_y']}px) scale({state['zoom']})"
        )

    def reset_transform() -> None:
        state.update(zoom=1.0, offset_x=0.0, offset_y=0.0, dragging=False)
        update_transform()

    def start_drag(event: GenericEventArguments) -> None:
        button = event.args.get("button")
        if button == 0:
            state.update(
                dragging=True,
                last_x=float(event.args["clientX"]),
                last_y=float(event.args["clientY"]),
            )
            image.classes(remove="cursor-grab", add="cursor-grabbing")
        elif button == 1:
            reset_transform()

    def handle_drag(event: GenericEventArguments) -> None:
        if not state["dragging"]:
            return
        current_x = float(event.args["clientX"])
        current_y = float(event.args["clientY"])
        state["offset_x"] += current_x - state["last_x"]
        state["offset_y"] += current_y - state["last_y"]
        state["last_x"] = current_x
        state["last_y"] = current_y
        update_transform()

    def end_drag(_: GenericEventArguments) -> None:
        state["dragging"] = False
        image.classes(remove="cursor-grabbing", add="cursor-grab")

    def handle_zoom(event: GenericEventArguments) -> None:
        factor = 1.1 if float(event.args["deltaY"]) < 0 else 0.9
        state["zoom"] = max(0.01, min(10.0, float(state["zoom"]) * factor))
        update_transform()

    image.on("mousedown", start_drag)
    image.on("mousemove", handle_drag)
    image.on("mouseup", end_drag)
    image.on("mouseleave", end_drag)
    image.on("wheel", handle_zoom)
    preview.on("close", preview.delete)
    preview.open()
    reset_transform()
