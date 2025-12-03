from setuptools import setup, find_packages

setup(
    name="mexcpy",
    version="0.1",
    packages=find_packages(),
    install_requires=[
        "aiohttp",
        "python-telegram-bot",
        "uvicorn",
        "fastapi",
        "python-dotenv",
        "telethon"
    ],
)