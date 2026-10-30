"""A sample module for code locator fixtures."""


def helper(value):
    return value * 2


class Widget:
    @staticmethod
    def build():
        return Widget()

    def render(self):
        return "widget"


def render():
    return "module-level"
