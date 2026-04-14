# UNI-T UTi120 USB-C Thermal Camera Webcam Daemon

- Command-line-only Python daemon based on the great work of [https://github.com/diegosainz/uni-t-thermal-uti120](https://github.com/diegosainz/uni-t-thermal-uti120)
- Runs in the background and provides the thermal camera image to a [v4l2loopback](https://github.com/v4l2loopback/v4l2loopback) "webcam"
- You can use it in (most) applications that expect a normal webcam, like video conferences or network streaming software
- No GUI, so you can for example set it up to a Raspberry Pi taped to a machine somewhere, and then watch it over network
- Most important features made available via command line options for scripting
- Improved processing with scaling and sharpening, plus (optional) CNN upscaling
- OSD rendering via pure OpenCV
- No more Qt and no extra Python libraries (v4l2loopback via ffmpeg binary)

## Installation

You need to install and load [v4l2loopback](https://github.com/v4l2loopback/v4l2loopback). You should be able to find information on how to load this for your Linux distribution online. Additionally, you need the  `ffmpeg` binary in your path. For Debian/Ubuntu and friends, something like this would be required:

```
sudo apt install v4l2loopback-dkms ffmpeg
sudo modprobe v4l2loopback video_nr=20 card_label=ThermalCamera exclusive_caps=1
```

If you want the CNN upscaling, you need to download the model [from Github](github.com/fannymonori/TF-ESPCN/blob/master/export/ESPCN_x4.pb). Otherwise,  `--upscaling_method cnn` will not work.

Otherwise, it is the same as in the [original readme](docs/README-old.md)

## Usage

Example:

```
python3 -m uti120 --show_center_temp --upscaling_method cnn --show_colorbar --palette Inferno --dev_video_file /dev/video20 
```

For playback, most applications that can use a webcam should just work, but some are a bit picky when it comes to formats, framerates or the ca. 1s pause for the periodic calibration. Make sure to have the daemon running and streaming before you try to access the webcam. `ffplay` seemed to behave well:

```
ffplay /dev/video20
```

General usage information:

```
$ python3 -m uti120 --help
usage: __main__.py [-h] [--dev_video_file DEV_VIDEO_FILE] [--show_min_max_temp | --no-show_min_max_temp] [--show_center_temp | --no-show_center_temp] [--show_colorbar | --no-show_colorbar] [--palette {Iron,Rainbow,Whitehot,Blackhot,Jet,Inferno}]
                   [--upscaling_method {trivial,simple,cnn}] [--rotate_deg ROTATE_DEG] [--flip | --no-flip] [--debug_ffmpeg | --no-debug_ffmpeg]
                   [--emissivity {Default,HumanSkin,Water,IceSnow,Concrete,BrickRed,WoodPlaned,Glass,Paper,Rubber,PlasticBlack,PaintFlat,FabricCloth,SoilEarth,Asphalt,OxidizedSteel,StainlessSteel,OxidizedCopper,AnodizedAluminum,PolishedMetal}]
                   [--emissivity_custom EMISSIVITY_CUSTOM] [--distance_m DISTANCE_M]

options:
  -h, --help            show this help message and exit
  --dev_video_file DEV_VIDEO_FILE
                        str (= /dev/video20)
  --show_min_max_temp, --no-show_min_max_temp
                        bool (= False)
  --show_center_temp, --no-show_center_temp
                        bool (= False)
  --show_colorbar, --no-show_colorbar
                        bool (= False)
  --palette {Iron,Rainbow,Whitehot,Blackhot,Jet,Inferno}
                        str (= Inferno)
  --upscaling_method {trivial,simple,cnn}
                        str (= trivial)
  --rotate_deg ROTATE_DEG
                        int (= 0)
  --flip, --no-flip     bool (= False)
  --debug_ffmpeg, --no-debug_ffmpeg
                        bool (= False)
  --emissivity {Default,HumanSkin,Water,IceSnow,Concrete,BrickRed,WoodPlaned,Glass,Paper,Rubber,PlasticBlack,PaintFlat,FabricCloth,SoilEarth,Asphalt,OxidizedSteel,StainlessSteel,OxidizedCopper,AnodizedAluminum,PolishedMetal}
                        str (= Default)
  --emissivity_custom EMISSIVITY_CUSTOM
                        float (= 0)
  --distance_m DISTANCE_M
                        float (= 1.0)
```

## License

This project is licensed under the [MIT License](LICENSE).

The disclaimers from the original Readme are kept here, for the sake of completeness:

```
The project was written mostly by an AI agent (Claude Code Opus 4.6) under my direction. I guided key decisions, provided the source materials (decompiled APK, Ghidra disassembly output), and conducted live hardware testing, but almost all code, including the reverse engineering analysis, protocol implementation, image processing pipeline, calibration system, and GUI, was written by the AI. I have not reviewed the code in detail for correctness or side effects. Review it carefully before using it for anything important.
```

```
This software was developed through clean-room reverse engineering of the USB protocol for interoperability purposes. It contains no code from the original manufacturer. The protocol was determined by decompiling the official Android APK and disassembling the native shared library using Ghidra, then validated through empirical testing with actual hardware.
```