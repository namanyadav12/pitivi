from gi.repository import GtkClutter
GtkClutter.init([])

from gi.repository import Gst, GES, GObject, Clutter, Gtk, GLib, Gdk

from pitivi.utils.timeline import Zoomable, Selection, UNSELECT
from pitivi.settings import GlobalSettings
from pitivi.dialogs.prefs import PreferencesDialog
from pitivi.utils.ui import EXPANDED_SIZE, SPACING
from ruler import ScaleRuler
from gettext import gettext as _
from pitivi.utils.pipeline import Pipeline
from layer import VideoLayerControl, AudioLayerControl
from elements import ClipElement, TransitionElement

GlobalSettings.addConfigOption('edgeSnapDeadband',
    section="user-interface",
    key="edge-snap-deadband",
    default=5,
    notify=True)

PreferencesDialog.addNumericPreference('edgeSnapDeadband',
    section=_("Behavior"),
    label=_("Snap distance"),
    description=_("Threshold (in pixels) at which two clips will snap together "
        "when dragging or trimming."),
    lower=0)

GlobalSettings.addConfigOption('imageClipLength',
    section="user-interface",
    key="image-clip-length",
    default=1000,
    notify=True)

PreferencesDialog.addNumericPreference('imageClipLength',
    section=_("Behavior"),
    label=_("Image clip duration"),
    description=_("Default clip length (in miliseconds) of images when inserting on the timeline."),
    lower=1)

# CONSTANTS

CONTROL_WIDTH = 250

# tooltip text for toolbar
DELETE = _("Delete Selected")
SPLIT = _("Split clip at playhead position")
KEYFRAME = _("Add a keyframe")
PREVKEYFRAME = _("Move to the previous keyframe")
NEXTKEYFRAME = _("Move to the next keyframe")
ZOOM_IN = _("Zoom In")
ZOOM_OUT = _("Zoom Out")
ZOOM_FIT = _("Zoom Fit")
UNGROUP = _("Ungroup clips")
GROUP = _("Group clips")
ALIGN = _("Align clips based on their soundtracks")
SELECT_BEFORE = ("Select all sources before selected")
SELECT_AFTER = ("Select all after selected")

ui = '''
<ui>
    <menubar name="MainMenuBar">
        <menu action="View">
            <placeholder name="Timeline">
                <menuitem action="ZoomIn" />
                <menuitem action="ZoomOut" />
                <menuitem action="ZoomFit" />
            </placeholder>
        </menu>
        <menu action="Timeline">
            <placeholder name="Timeline">
                <menuitem action="Split" />
                <menuitem action="DeleteObj" />
                <separator />
                <menuitem action="GroupObj" />
                <menuitem action="UngroupObj" />
                <menuitem action="AlignObj" />
                <separator />
                <menuitem action="Keyframe" />
                <menuitem action="Prevkeyframe" />
                <menuitem action="Nextkeyframe" />
                <separator />
                <menuitem action="PlayPause" />
                <menuitem action="Screenshot" />
            </placeholder>
        </menu>
    </menubar>
    <toolbar name="TimelineToolBar">
        <placeholder name="Timeline">
            <separator />
            <toolitem action="Split" />
            <toolitem action="DeleteObj" />
            <toolitem action="GroupObj" />
            <toolitem action="UngroupObj" />
            <toolitem action="AlignObj" />
        </placeholder>
    </toolbar>
    <accelerator action="PlayPause" />
    <accelerator action="DeleteObj" />
    <accelerator action="ControlEqualAccel" />
    <accelerator action="ControlKPAddAccel" />
    <accelerator action="ControlKPSubtractAccel" />
    <accelerator action="Keyframe" />
</ui>
'''


"""
Convention throughout this file:
Every GES element which name could be mistaken with a UI element
is prefixed with a little b, example : bTimeline
"""


class TimelineStage(Clutter.ScrollActor, Zoomable):
    __gsignals__ = {
        'scrolled': (GObject.SIGNAL_RUN_FIRST, None, ())
    }

    def __init__(self, container):
        Clutter.ScrollActor.__init__(self)
        Zoomable.__init__(self)
        self.bTimeline = None
        self.set_background_color(Clutter.Color.new(31, 30, 33, 255))
        self.elements = []
        self.selection = Selection()
        self._createPlayhead()
        self._createSnapIndicator()
        self._container = container
        self.lastPosition = 0
        self._scroll_point = Clutter.Point()

    # Public API

    def setPipeline(self, pipeline):
        pipeline.connect('position', self._positionCb)

    def setTimeline(self, bTimeline):
        """
        @param bTimeline : the backend GES.Timeline which we interface.
        Does all the necessary connections.
        """

        if self.bTimeline is not None:
            self.bTimeline.disconnect_by_func(self._trackAddedCb)
            self.bTimeline.disconnect_by_func(self._trackRemovedCb)
            self.bTimeline.disconnect_by_func(self._layerAddedCb)
            self.bTimeline.disconnect_by_func(self._layerRemovedCb)
            self.bTimeline.disconnect_by_func(self._snapCb)
            self.bTimeline.disconnect_by_func(self._snapEndedCb)

        self.bTimeline = bTimeline

        for track in bTimeline.get_tracks():
            self._connectTrack(track)
        for layer in bTimeline.get_layers():
            self._add_layer(layer)

        self.bTimeline.connect("track-added", self._trackAddedCb)
        self.bTimeline.connect("track-removed", self._trackRemovedCb)
        self.bTimeline.connect("layer-added", self._layerAddedCb)
        self.bTimeline.connect("layer-removed", self._layerRemovedCb)
        self.bTimeline.connect("snapping-started", self._snapCb)
        self.bTimeline.connect("snapping-ended", self._snapEndedCb)

        self.zoomChanged()

    #Stage was clicked with nothing under the pointer
    def emptySelection(self):
        """
        Empty the current selection.
        """
        self.selection.setSelection(self.selection.getSelectedTrackElements(), UNSELECT)

    def findBrother(self, element):
        father = element.get_parent()
        for elem in self.elements:
            if elem.bElement.get_parent() == father and elem.bElement != element:
                return elem
        return None

    #Internal API

    def _connectTrack(self, track):
        track.connect("track-element-added", self._trackElementAddedCb)
        track.connect("track-element-removed", self._trackElementRemovedCb)

    def _disconnectTrack(self, track):
        track.disconnect_by_func(self._trackElementAddedCb)
        track.disconnect_by_func(self._trackElementRemovedCb)

    def _positionCb(self, pipeline, position):
        self.playhead.props.x = self.nsToPixel(position)
        self._container._scrollToPlayhead()
        self.lastPosition = position

    def _updatePlayHead(self):
        height = len(self.bTimeline.get_layers()) * (EXPANDED_SIZE + SPACING) * 2
        self.playhead.set_size(2, height)

    def _createPlayhead(self):
        self.playhead = Clutter.Actor()
        self.playhead.set_background_color(Clutter.Color.new(200, 0, 0, 255))
        self.playhead.set_size(0, 0)
        self.playhead.set_position(0, 0)
        self.add_child(self.playhead)
        self.playhead.set_easing_duration(0)
        self.playhead.set_z_position(1)

    def _createSnapIndicator(self):
        self._snap_indicator = Clutter.Actor()
        self._snap_indicator.set_background_color(Clutter.Color.new(0, 0, 250, 200))
        self._snap_indicator.props.visible = False
        self._snap_indicator.props.width = 3
        self._snap_indicator.props.y = 0
        self.add_child(self._snap_indicator)

    def _addTimelineElement(self, track, bElement):
        if isinstance(bElement.get_parent(), GES.TransitionClip):
            element = TransitionElement(bElement, track, self)
            element.set_z_position(0)
        else:
            element = ClipElement(bElement, track, self)
            element.set_z_position(-1)

        bElement.connect("notify::start", self._elementStartChangedCb, element)
        bElement.connect("notify::duration", self._elementDurationChangedCb, element)
        bElement.connect("notify::in-point", self._elementInPointChangedCb, element)
        bElement.connect("notify::priority", self._elementPriorityChangedCb, element)

        self.elements.append(element)

        self._setElementY(element)

        self.add_child(element)

        self._setElementX(element)

    def _removeTimelineElement(self, track, bElement):
        bElement.disconnect_by_func(self._elementStartChangedCb)
        bElement.disconnect_by_func(self._elementDurationChangedCb)
        bElement.disconnect_by_func(self._elementInPointChangedCb)
        for element in self.elements:
            if element.bElement == bElement:
                break
        self.elements.remove(element)
        self.remove_child(element)

    def _setElementX(self, element, ease=True):
        if ease:
            element.save_easing_state()
            element.set_easing_duration(600)
        element.props.x = self.nsToPixel(element.bElement.get_start())
        if ease:
            element.restore_easing_state()

    # Crack, change that when we have retractable layers
    def _setElementY(self, element):
        element.save_easing_state()
        y = 0
        bElement = element.bElement
        track_type = bElement.get_track_type()

        if (track_type == GES.TrackType.AUDIO):
            y = len(self.bTimeline.get_layers()) * (EXPANDED_SIZE + SPACING)

        y += bElement.get_parent().get_layer().get_priority() * (EXPANDED_SIZE + SPACING) + SPACING

        element.props.y = y
        element.restore_easing_state()

    def _updateSize(self):
        self.save_easing_state()
        self.set_easing_duration(0)
        self.props.width = self.nsToPixel(self.bTimeline.get_duration()) + 250
        self.restore_easing_state()
        self._container.updateHScrollAdjustments()

    def _redraw(self):
        self._updateSize()
        self.save_easing_state()
        for element in self.elements:
            self._setElementX(element)
        self.restore_easing_state()
        self.playhead.props.x = self.nsToPixel(self.lastPosition)

    # Interface overrides (Zoomable)

    def zoomChanged(self):
        self._redraw()

    def _add_layer(self, layer):
        for element in self.elements:
            self._setElementY(element)
        self.save_easing_state()
        self.props.height = (len(self.bTimeline.get_layers()) + 1) * (EXPANDED_SIZE + SPACING) * 2 + SPACING
        self.restore_easing_state()
        self._container.vadj.props.upper = self.props.height
        self._container.controls.addLayerControl(layer)
        self._updatePlayHead()

    # Clutter Override

    # TODO: remove self._scroll_point and get_scroll_point as soon as the Clutter API
    # offers a way to query a ScrollActor for its current scroll point
    def scroll_to_point(self, point):
        Clutter.ScrollActor.scroll_to_point(self, point)
        self._scroll_point = point.copy()
        self.emit("scrolled")

    def get_scroll_point(self):
        return self._scroll_point

    # Callbacks

    # snapping indicator
    def _snapCb(self, unused_timeline, obj1, obj2, position):
        """
        Display or hide a snapping indicator line
        """
        if position == 0:
            self._snapEndedCb()
        else:
            self._snap_indicator.props.x = Zoomable.nsToPixel(position)
            height = len(self.bTimeline.get_layers()) * (EXPANDED_SIZE + SPACING) * 2
            self._snap_indicator.props.height = height
            self._snap_indicator.props.visible = True

    def _snapEndedCb(self, *args):
        self._snap_indicator.props.visible = False

    def _layerAddedCb(self, timeline, layer):
        self._add_layer(layer)

    def _layerRemovedCb(self, timeline, layer):
        layer.disconnect_by_func(self._clipAddedCb)
        layer.disconnect_by_func(self._clipRemovedCb)
        self._updatePlayHead()

    def _clipAddedCb(self, layer, clip):
        clip.connect("child-added", self._elementAddedCb)
        clip.connect("child-removed", self._elementRemovedCb)

    def _clipRemovedCb(self, layer, clip):
        clip.disconnect_by_func(self._elementAddedCb)
        clip.disconnect_by_func(self._elementRemovedCb)

    def _trackAddedCb(self, timeline, track):
        self._connectTrack(track)

    def _trackRemovedCb(self, timeline, track):
        self._disconnectTrack(track)

    def _elementAddedCb(self, clip, bElement):
        pass

    def _elementRemovedCb(self):
        pass

    def _trackElementAddedCb(self, track, bElement):
        self._updateSize()
        self._addTimelineElement(track, bElement)

    def _trackElementRemovedCb(self, track, bElement):
        self._removeTimelineElement(track, bElement)

    def _elementPriorityChangedCb(self, bElement, priority, element):
        self._setElementY(element)

    def _elementStartChangedCb(self, bElement, start, element):
        self._updateSize()
        if element.isDragged:
            self._setElementX(element, ease=False)
        else:
            self._setElementX(element)

    def _elementDurationChangedCb(self, bElement, duration, element):
        self._updateSize()
        element.update(False)

    def _elementInPointChangedCb(self, bElement, inpoint, element):
        self._setElementX(element, ease=False)

    def _layerPriorityChangedCb(self, layer, priority):
        self._redraw()


def quit_(stage):
    Gtk.main_quit()


def quit2_(*args, **kwargs):
    Gtk.main_quit()


class ZoomBox(Gtk.HBox, Zoomable):
    def __init__(self, timeline):
        """
        This will hold the widgets responsible for zooming.
        """
        Gtk.HBox.__init__(self)
        Zoomable.__init__(self)

        self.timeline = timeline

        zoom_fit_btn = Gtk.Button()
        zoom_fit_btn.set_relief(Gtk.ReliefStyle.NONE)
        zoom_fit_btn.set_tooltip_text(ZOOM_FIT)
        zoom_fit_icon = Gtk.Image()
        zoom_fit_icon.set_from_stock(Gtk.STOCK_ZOOM_FIT, Gtk.IconSize.BUTTON)
        zoom_fit_btn_hbox = Gtk.HBox()
        zoom_fit_btn_hbox.pack_start(zoom_fit_icon, False, True, 0)
        zoom_fit_btn_hbox.pack_start(Gtk.Label(_("Zoom")), False, True, 0)
        zoom_fit_btn.add(zoom_fit_btn_hbox)
        zoom_fit_btn.connect("clicked", self._zoomFitCb)
        self.pack_start(zoom_fit_btn, False, True, 0)

        # zooming slider
        self._zoomAdjustment = Gtk.Adjustment()
        self._zoomAdjustment.set_value(Zoomable.getCurrentZoomLevel())
        self._zoomAdjustment.connect("value-changed", self._zoomAdjustmentChangedCb)
        self._zoomAdjustment.props.lower = 0
        self._zoomAdjustment.props.upper = Zoomable.zoom_steps
        zoomslider = Gtk.Scale.new(Gtk.Orientation.HORIZONTAL, adjustment=self._zoomAdjustment)
        zoomslider.props.draw_value = False
        zoomslider.set_tooltip_text(_("Zoom Timeline"))
        zoomslider.connect("scroll-event", self._zoomSliderScrollCb)
        zoomslider.set_size_request(100, 0)  # At least 100px wide for precision
        self.pack_start(zoomslider, True, True, 0)

        self.show_all()

        self._updateZoomSlider = True

    def _zoomAdjustmentChangedCb(self, adjustment):
        # GTK crack
        self._updateZoomSlider = False
        Zoomable.setZoomLevel(int(adjustment.get_value()))
        self.zoomed_fitted = False
        self._updateZoomSlider = True

    def _zoomFitCb(self, button):
        self.timeline.zoomFit()

    def _zoomSliderScrollCb(self, unused, event):
        value = self._zoomAdjustment.get_value()
        if event.direction in [Gdk.ScrollDirection.UP, Gdk.ScrollDirection.RIGHT]:
            self._zoomAdjustment.set_value(value + 1)
        elif event.direction in [Gdk.ScrollDirection.DOWN, Gdk.ScrollDirection.LEFT]:
            self._zoomAdjustment.set_value(value - 1)

    def zoomChanged(self):
        if self._updateZoomSlider:
            self._zoomAdjustment.set_value(self.getCurrentZoomLevel())


class ControlActor(GtkClutter.Actor):
    def __init__(self, container, widget, layer):
        GtkClutter.Actor.__init__(self)
        self.get_widget().add(widget)
        self.set_reactive(True)
        self.layer = layer
        self._setUpDragAndDrop()
        self._container = container
        self.widget = widget

    def _getLayerForY(self, y):
        if self.isAudio:
            y -= self.nbrLayers * (EXPANDED_SIZE + SPACING)
        priority = int(y / (EXPANDED_SIZE + SPACING))
        return priority

    def _setUpDragAndDrop(self):
        self.dragAction = Clutter.DragAction()
        self.add_action(self.dragAction)
        self.dragAction.connect("drag-begin", self._dragBeginCb)
        self.dragAction.connect("drag-progress", self._dragProgressCb)
        self.dragAction.connect("drag-end", self._dragEndCb)

    def _dragBeginCb(self, action, actor, event_x, event_y, modifiers):
        self.brother = self._container.getBrotherControl(self)
        self.brother.raise_top()
        self.raise_top()
        self.nbrLayers = len(self._container.timeline.bTimeline.get_layers())
        self._dragBeginStartX = event_x

    def _dragProgressCb(self, action, actor, delta_x, delta_y):
        y = self.dragAction.get_motion_coords()[1]
        priority = self._getLayerForY(y)
        lowerLimit = 0
        if self.isAudio:
            lowerLimit = self.nbrLayers * (EXPANDED_SIZE + SPACING)

        if actor.props.y + delta_y > lowerLimit and priority < self.nbrLayers:
            actor.move_by(0, delta_y)
            self.brother.move_by(0, delta_y)

        if self.layer.get_priority() != priority and priority >= 0 and priority < self.nbrLayers:
            self._container.moveLayer(self, priority)
        return False

    def _dragEndCb(self, action, actor, event_x, event_y, modifiers):
        priority = self._getLayerForY(event_y)
        if self.layer.get_priority() != priority and priority >= 0 and priority < self.nbrLayers:
            self._container.moveLayer(self, priority)
        self._container._reorderLayerActors()


class ControlContainer(Clutter.ScrollActor):
    __gsignals__ = {
        "selection-changed": (GObject.SignalFlags.RUN_LAST, None, (GObject.TYPE_PYOBJECT,),)
    }

    def __init__(self, timeline):
        Clutter.ScrollActor.__init__(self)
        self.controlActors = []
        self.trackControls = []
        self.timeline = timeline

    def _setTrackControlPosition(self, control):
        y = control.layer.get_priority() * (EXPANDED_SIZE + SPACING) + SPACING
        if control.isAudio:
            y += len(self.timeline.bTimeline.get_layers()) * (EXPANDED_SIZE + SPACING)
        control.set_position(0, y)

    def _reorderLayerActors(self):
        for control in self.controlActors:
            control.save_easing_state()
            control.set_easing_mode(Clutter.AnimationMode.EASE_OUT_BACK)
            self._setTrackControlPosition(control)
            control.restore_easing_state()

    def getBrotherControl(self, control):
        for cont in self.controlActors:
            if cont != control and cont.layer == control.layer:
                return cont

    def moveLayer(self, control, target):
        movedLayer = control.layer
        priority = movedLayer.get_priority()
        self.timeline.bTimeline.enable_update(False)
        movedLayer.props.priority = 999

        if priority > target:
            for layer in self.timeline.bTimeline.get_layers():
                prio = layer.get_priority()
                if target <= prio < priority:
                    layer.props.priority = prio + 1
        elif priority < target:
            for layer in self.timeline.bTimeline.get_layers():
                prio = layer.get_priority()
                if priority < prio <= target:
                    layer.props.priority = prio - 1
        movedLayer.props.priority = target

        self._reorderLayerActors()
        self.timeline.bTimeline.enable_update(True)

    def addTrackControl(self, layer, isAudio):
        if isAudio:
            control = AudioLayerControl(self, layer)
        else:
            control = VideoLayerControl(self, layer)

        controlActor = ControlActor(self, control, layer)
        controlActor.isAudio = isAudio
        controlActor.layer = layer
        controlActor.set_size(CONTROL_WIDTH, EXPANDED_SIZE + SPACING)

        self.add_child(controlActor)
        self.trackControls.append(control)
        self.controlActors.append(controlActor)

    def selectLayerControl(self, layer_control):
        for control in self.trackControls:
            control.selected = False
        layer_control.selected = True
        self.props.height += (EXPANDED_SIZE + SPACING) * 2 + SPACING

    def addLayerControl(self, layer):
        self.addTrackControl(layer, False)
        self.addTrackControl(layer, True)
        self._reorderLayerActors()


class Timeline(Gtk.VBox, Zoomable):
    def __init__(self, instance, ui_manager):
        gtksettings = Gtk.Settings.get_default()
        gtksettings.set_property("gtk-application-prefer-dark-theme", True)
        Zoomable.__init__(self)
        Gtk.VBox.__init__(self)
        GObject.threads_init()

        self.ui_manager = ui_manager
        self.app = instance
        self._settings = self.app.settings

        self.embed = GtkClutter.Embed()
        self.embed.show()

        self.point = Clutter.Point()
        self.point.x = 0
        self.point.y = 0

        self.zoomBox = ZoomBox(self)

        self._packScrollbars(self)

        stage = self.embed.get_stage()
        stage.set_background_color(Clutter.Color.new(31, 30, 33, 255))

        self.stage = stage

        self.embed.connect("scroll-event", self._scrollEventCb)

        self.stage.set_throttle_motion_events(True)

        stage.show()

        widget = TimelineStage(self)

        self.controls = ControlContainer(widget)
        stage.add_child(self.controls)
        self.controls.set_position(0, 0)
        self.controls.set_z_position(2)

        stage.add_child(widget)
        widget.set_position(CONTROL_WIDTH, 0)
        stage.connect("destroy", quit_)
        stage.connect("button-press-event", self._clickedCb)
        self.timeline = widget

        self.scrolled = 0

        self._createActions()

        self._projectmanager = None
        self._project = None

        self._settings.connect("edgeSnapDeadbandChanged",
                self._snapDistanceChangedCb)

        self.show_all()

    def insertEnd(self, assets):
        """
        Add source at the end of the timeline
        @type sources: An L{GES.TimelineSource}
        @param x2: A list of sources to add to the timeline
        """
        self.app.action_log.begin("add clip")
        # FIXME we should find the longets layer instead of adding it to the
        # first one
        # Handle the case of a blank project
        layer = self._ensureLayer()[0]
        self.bTimeline.enable_update(False)
        for asset in assets:
            if isinstance(asset, GES.TitleClip):
                clip_duration = asset.get_duration()
            elif asset.is_image():
                clip_duration = long(long(self._settings.imageClipLength) * Gst.SECOND / 1000)
            else:
                clip_duration = asset.get_duration()

            print "added asset"
            if not isinstance(asset, GES.TitleClip):
                layer.add_asset(asset, self.bTimeline.props.duration,
                                0, clip_duration, 1.0, asset.get_supported_formats())
            else:
                asset.set_start(self.bTimeline.props.duration)
                layer.add_clip(asset)
        self.bTimeline.enable_update(True)

    def setProjectManager(self, projectmanager):
        if self._projectmanager is not None:
            self._projectmanager.disconnect_by_func(self._projectChangedCb)

        self._projectmanager = projectmanager
        if projectmanager is not None:
            projectmanager.connect("new-project-created", self._projectCreatedCb)
            projectmanager.connect("new-project-loaded", self._projectChangedCb)

    def _ensureLayer(self):
        """
        Make sure we have a layer in our timeline

        Returns: The number of layer present in self.timeline
        """
        layers = self.bTimeline.get_layers()

        if (len(layers) == 0):
            layer = GES.Layer()
            layer.props.auto_transition = True
            self.bTimeline.add_layer(layer)
            layers = [layer]

        return layers

    def _createActions(self):
        actions = (
            ("ZoomIn", Gtk.STOCK_ZOOM_IN, None,
            "<Control>plus", ZOOM_IN, self._zoomInCb),

            ("ZoomOut", Gtk.STOCK_ZOOM_OUT, None,
            "<Control>minus", ZOOM_OUT, self._zoomOutCb),

            ("ZoomFit", Gtk.STOCK_ZOOM_FIT, None,
            "<Control>0", ZOOM_FIT, self._zoomFitCb),

            ("Screenshot", None, _("Export current frame..."),
            None, _("Export the frame at the current playhead "
                    "position as an image file."), self._screenshotCb),

            # Alternate keyboard shortcuts to the actions above
            ("ControlEqualAccel", Gtk.STOCK_ZOOM_IN, None,
            "<Control>equal", ZOOM_IN, self._zoomInCb),

            ("ControlKPAddAccel", Gtk.STOCK_ZOOM_IN, None,
            "<Control>KP_Add", ZOOM_IN, self._zoomInCb),

            ("ControlKPSubtractAccel", Gtk.STOCK_ZOOM_OUT, None,
            "<Control>KP_Subtract", ZOOM_OUT, self._zoomOutCb),
        )

        selection_actions = (
            ("DeleteObj", Gtk.STOCK_DELETE, None,
            "Delete", DELETE, self.deleteSelected),

            ("UngroupObj", "pitivi-ungroup", _("Ungroup"),
            "<Shift><Control>G", UNGROUP, self.ungroupSelected),

            # Translators: This is an action, the title of a button
            ("GroupObj", "pitivi-group", _("Group"),
            "<Control>G", GROUP, self.groupSelected),

            ("AlignObj", "pitivi-align", _("Align"),
            "<Shift><Control>A", ALIGN, self.alignSelected),
        )

        playhead_actions = (
            ("PlayPause", Gtk.STOCK_MEDIA_PLAY, None,
            "space", _("Start Playback"), self.playPause),

            ("Split", "pitivi-split", _("Split"),
            "S", SPLIT, self.split),

            ("Keyframe", "pitivi-keyframe", _("Add a Keyframe"),
            "K", KEYFRAME, self.keyframe),

            ("Prevkeyframe", None, _("_Previous Keyframe"),
            "comma", PREVKEYFRAME, self._previousKeyframeCb),

            ("Nextkeyframe", None, _("_Next Keyframe"),
            "period", NEXTKEYFRAME, self._nextKeyframeCb),
        )

        actiongroup = Gtk.ActionGroup("timelinepermanent")
        actiongroup.add_actions(actions)
        self.ui_manager.insert_action_group(actiongroup, 0)

        self.selection_actions = Gtk.ActionGroup("timelineselection")
        self.selection_actions.add_actions(selection_actions)
        self.selection_actions.set_sensitive(False)
        self.ui_manager.insert_action_group(self.selection_actions, -1)
        self.playhead_actions = Gtk.ActionGroup("timelineplayhead")
        self.playhead_actions.add_actions(playhead_actions)
        self.ui_manager.insert_action_group(self.playhead_actions, -1)

        self.ui_manager.add_ui_from_string(ui)

    def _packScrollbars(self, vbox):
        self.hadj = Gtk.Adjustment()
        self.vadj = Gtk.Adjustment()
        self.hadj.connect("value-changed", self._updateScrollPosition)
        self.vadj.connect("value-changed", self._updateScrollPosition)

        self._vscrollbar = Gtk.VScrollbar(self.vadj)

        self._hscrollBar = Gtk.HScrollbar(self.hadj)
        vbox.pack_end(self._hscrollBar, False, True, False)

        self.ruler = ScaleRuler(self, self.hadj)
        self.ruler.setProjectFrameRate(24.)

        self.ruler.set_size_request(0, 25)
        self.ruler.hide()

        self.vadj.props.lower = 0
        self.vadj.props.upper = 500
        self.vadj.props.page_size = 250

        hbox = Gtk.HBox()
        hbox.set_size_request(-1, 500)
        hbox.pack_start(self.embed, True, True, True)
        hbox.pack_start(self._vscrollbar, False, True, False)

        vbox.pack_end(hbox, True, True, True)

        hbox = Gtk.HBox()
        self.zoomBox.set_size_request(CONTROL_WIDTH, -1)
        hbox.pack_start(self.zoomBox, False, True, False)
        hbox.pack_start(self.ruler, True, True, True)

        vbox.pack_end(hbox, False, True, False)

    def _updateScrollPosition(self, adjustment):
        self._scroll_pos_ns = Zoomable.pixelToNs(self.hadj.get_value())
        point = Clutter.Point()
        point.x = self.hadj.get_value()
        point.y = self.vadj.get_value()
        self.point = point
        self.timeline.scroll_to_point(point)
        point.x = 0
        self.controls.scroll_to_point(point)

    def zoomChanged(self):
        if self._settings and self.bTimeline:
            # zoomChanged might be called various times before the UI is ready
            self.bTimeline.props.snapping_distance = \
                Zoomable.pixelToNs(self._settings.edgeSnapDeadband)
        self.updateHScrollAdjustments()

    def updateHScrollAdjustments(self):
        """
        Recalculate the horizontal scrollbar depending on the timeline duration.
        """
        timeline_ui_width = self.embed.get_allocation().width
#        controls_width = self.controls.get_allocation().width
#        scrollbar_width = self._vscrollbar.get_allocation().width
        controls_width = 0
        scrollbar_width = 0
        contents_size = Zoomable.nsToPixel(self.bTimeline.props.duration)

        widgets_width = controls_width + scrollbar_width
        end_padding = 500  # Provide some space for clip insertion at the end

        self.hadj.props.lower = 0
        self.hadj.props.upper = contents_size + widgets_width + end_padding
        self.hadj.props.page_size = timeline_ui_width
        self.hadj.props.page_increment = contents_size * 0.9
        self.hadj.props.step_increment = contents_size * 0.1

        if contents_size + widgets_width <= timeline_ui_width:
            # We're zoomed out completely, re-enable automatic zoom fitting
            # when adding new clips.
            #self.log("Setting 'zoomed_fitted' to True")
            self.zoomed_fitted = True

    def run(self):
        self.testTimeline(self.timeline)
        GLib.io_add_watch(sys.stdin, GLib.IO_IN, quit2_)
        Gtk.main()

    def _setBestZoomRatio(self):
        """
        Set the zoom level so that the entire timeline is in view.
        """
        ruler_width = self.ruler.get_allocation().width
        # Add Gst.SECOND - 1 to the timeline duration to make sure the
        # last second of the timeline will be in view.
        duration = self.timeline.bTimeline.get_duration()
        if duration == 0:
#            self.debug("The timeline duration is 0, impossible to calculate zoom")
            return

        timeline_duration = duration + Gst.SECOND - 1
        timeline_duration_s = int(timeline_duration / Gst.SECOND)

        #self.debug("duration: %s, timeline duration: %s" % (print_ns(duration),
    #       print_ns(timeline_duration)))

        ideal_zoom_ratio = float(ruler_width) / timeline_duration_s
        nearest_zoom_level = Zoomable.computeZoomLevel(ideal_zoom_ratio)
        #self.debug("Ideal zoom: %s, nearest_zoom_level %s", ideal_zoom_ratio, nearest_zoom_level)
        Zoomable.setZoomLevel(nearest_zoom_level)
        #self.timeline.props.snapping_distance = \
        #    Zoomable.pixelToNs(self.app.settings.edgeSnapDeadband)

        # Only do this at the very end, after updating the other widgets.
        #self.log("Setting 'zoomed_fitted' to True")
        self.zoomed_fitted = True

    def zoomFit(self):
        self._hscrollBar.set_value(0)
        self._setBestZoomRatio()

    def scrollToPosition(self, position):
        if position > self.hadj.props.upper:
            # we can't perform the scroll because the canvas needs to be
            # updated
            GLib.idle_add(self._scrollToPosition, position)
        else:
            self._scrollToPosition(position)

    def _scrollLeft(self):
        self._hscrollBar.set_value(self._hscrollBar.get_value() -
            self.hadj.props.page_size ** (2.0 / 3.0))

    def _scrollRight(self):
        self._hscrollBar.set_value(self._hscrollBar.get_value() +
            self.hadj.props.page_size ** (2.0 / 3.0))

    def _scrollUp(self):
        self._vscrollbar.set_value(self._vscrollbar.get_value() -
            self.vadj.props.page_size ** (2.0 / 3.0))

    def _scrollDown(self):
        self._vscrollbar.set_value(self._vscrollbar.get_value() +
            self.vadj.props.page_size ** (2.0 / 3.0))

    def _scrollToPosition(self, position):
        self._hscrollBar.set_value(position)
        return False

    def _scrollToPlayhead(self):
        canvas_size = self.embed.get_allocation().width - CONTROL_WIDTH
        new_pos = self.timeline.playhead.props.x
        scroll_pos = self.hadj.get_value()
        self.scrollToPosition(min(new_pos - canvas_size / 2,
                                  self.hadj.props.upper - canvas_size - 1))

    def goToPoint(self, timeline):
        point = Clutter.Point()
        point.x = 1000
        point.y = 0
        timeline.scroll_to_point(point)
        return False

    def addClipToLayer(self, layer, asset, start, duration, inpoint):
        layer.add_asset(asset, start * Gst.SECOND, 0, duration * Gst.SECOND, 1.0, asset.get_supported_formats())

    def handle_message(self, bus, message):
        if message.type == Gst.MessageType.ELEMENT:
            if message.has_name('prepare-window-handle'):
                Gdk.threads_enter()
                self.sink = message.src
                self.sink.set_window_handle(self.viewer.window_xid)
                self.sink.expose()
                Gdk.threads_leave()
            elif message.type == Gst.MessageType.STATE_CHANGED:
                prev, new, pending = message.parse_state_changed()
        return True

    def _clickedCb(self, stage, event):
        actor = self.stage.get_actor_at_pos(Clutter.PickMode.REACTIVE, event.x, event.y)
        if actor == stage:
            self.timeline.emptySelection()

    def doSeek(self):
        #self.pipeline.simple_seek(3000000000)
        return False

    def togglePlayback(self, button):
        self.pipeline.togglePlayback()

    def _renderingSettingsChangedCb(self, project, item, value):
        """
        Called when any Project metadata changes, we filter out the one
        we are interested in.

        if @item is None, it mean we called it ourself, and want to force
        getting the project videorate value
        """
        if item == "videorate" or item is None:
            if value is None:
                value = project.videorate
            self._framerate = value
            self.ruler.setProjectFrameRate(self._framerate)

    def _doAssetAddedCb(self, project, asset, layer):
        self.addClipToLayer(layer, asset, 2, 10, 5)
        self.addClipToLayer(layer, asset, 15, 10, 5)

        self.pipeline = Pipeline()
        self.pipeline.add_timeline(layer.get_timeline())

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self.handle_message)
        self.playButton.connect("clicked", self.togglePlayback)
        #self.pipeline.togglePlayback()
        self.pipeline.activatePositionListener(interval=30)
        self.timeline.setPipeline(self.pipeline)
        GObject.timeout_add(1000, self.doSeek)
        Zoomable.setZoomLevel(50)

    def _snapDistanceChangedCb(self, settings):
        if self.bTimeline:
            self.bTimeline.props.snapping_distance = \
                Zoomable.pixelToNs(settings.edgeSnapDeadband)

    def _projectChangedCb(self, app, project, unused_fully_loaded):
        """
        When a project is loaded, we connect to its pipeline
        """
#        self.debug("Project changed")

        if project:
#            self.debug("Project is not None, connecting to its pipeline")
            self._seeker = self._project.seeker
            self._pipeline = self._project.pipeline
#            self._pipeline.connect("position", self.positionChangedCb)
            self.ruler.setProjectFrameRate(self._project.videorate)
            self.ruler.zoomChanged()
            self._renderingSettingsChangedCb(self._project, None, None)

            self._setBestZoomRatio()

    def _projectCreatedCb(self, app, project):
        """
        When a project is created, we connect to it timeline
        """
#        self.debug("Setting project %s", project)
        if self._project:
            self._project.disconnect_by_func(self._renderingSettingsChangedCb)
            #try:
            #    self._pipeline.disconnect_by_func(self.positionChangedCb)
            #except TypeError:
            #    pass  # We were not connected no problem

            self._pipeline = None
            self._seeker = None

        self._project = project
        if self._project:
            self._project.connect("rendering-settings-changed",
                                  self._renderingSettingsChangedCb)
            self.setTimeline(project.timeline)

    def _zoomInCb(self, unused_action):
        # This only handles the button callbacks (from the menus),
        # not keyboard shortcuts or the zoom slider!
        Zoomable.zoomIn()
        self.log("Setting 'zoomed_fitted' to False")
        self.zoomed_fitted = False

    def _zoomOutCb(self, unused_action):
        # This only handles the button callbacks (from the menus),
        # not keyboard shortcuts or the zoom slider!
        Zoomable.zoomOut()
        self.log("Setting 'zoomed_fitted' to False")
        self.zoomed_fitted = False

    def _zoomFitCb(self, unused, unsued2=None):
        self._setBestZoomRatio()

    def _screenshotCb(self, unused_action):
        """
        Export a snapshot of the current frame as an image file.
        """
        foo = self._showSaveScreenshotDialog()
        if foo:
            path, mime = foo[0], foo[1]
            self._project.pipeline.save_thumbnail(-1, -1, mime, path)

    def deleteSelected(self, unused_action):
        if self.timeline:
            self.app.action_log.begin("delete clip")
            #FIXME GES port: Handle unlocked TrackElement-s
            for clip in self.timeline.selection:
                layer = clip.get_layer()
                layer.remove_clip(clip)
            self.app.action_log.commit()

    def ungroupSelected(self, unused_action):
        if self.timeline:
            self.debug("Ungouping selected clips %s" % self.timeline.selection)
            self.timeline.enable_update(False)
            self.app.action_log.begin("ungroup")
            for clip in self.timeline.selection:
                clip.ungroup(False)
            self.timeline.enable_update(True)
            self.app.action_log.commit()

    def groupSelected(self, unused_action):
        if self.timeline:
            self.debug("Gouping selected clips %s" % self.timeline.selection)
            self.timeline.enable_update(False)
            self.app.action_log.begin("group")
            GES.Container.group(self.timeline.selection)
            self.app.action_log.commit()
            self.timeline.enable_update(True)

    def alignSelected(self, unused_action):
        if "NumPy" in missing_soft_deps:
            DepsManager(self.app)

        elif self.timeline:
            progress_dialog = AlignmentProgressDialog(self.app)
            progress_dialog.window.show()
            self.app.action_log.begin("align")
            self.timeline.enable_update(False)

            def alignedCb():  # Called when alignment is complete
                self.timeline.enable_update(True)
                self.app.action_log.commit()
                progress_dialog.window.destroy()

            pmeter = self.timeline.alignSelection(alignedCb)
            pmeter.addWatcher(progress_dialog.updatePosition)

    def split(self, action):
        """
        Split clips at the current playhead position, regardless of selections.
        """
        self.bTimeline.enable_update(False)
        position = self.app.current.pipeline.getPosition()
        for track in self.bTimeline.get_tracks():
            for element in track.get_elements():
                start = element.get_start()
                end = start + element.get_duration()
                if start < position and end > position:
                    clip = element.get_parent()
                    clip.split(position)
        self.bTimeline.enable_update(True)

    def keyframe(self, action):
        """
        Add or remove a keyframe at the current position of the selected clip.

        FIXME GES: this method is currently not used anywhere
        """
        selected = self.timeline.selection.getSelectedTrackElements()
        for obj in selected:
            keyframe_exists = False
            position = self.app.current.pipeline.getPosition()
            position_in_obj = (position - obj.start) + obj.in_point
            interpolators = obj.getInterpolators()
            for value in interpolators:
                interpolator = obj.getInterpolator(value)
                keyframes = interpolator.getInteriorKeyframes()
                for kf in keyframes:
                    if kf.getTime() == position_in_obj:
                        keyframe_exists = True
                        self.app.action_log.begin("remove volume point")
                        interpolator.removeKeyframe(kf)
                        self.app.action_log.commit()
                if keyframe_exists is False:
                    self.app.action_log.begin("add volume point")
                    interpolator.newKeyframe(position_in_obj)
                    self.app.action_log.commit()

    def _previousKeyframeCb(self, action):
        position = self.app.current.pipeline.getPosition()
        prev_kf = self.timeline.getPrevKeyframe(position)
        if prev_kf:
            self._seeker.seek(prev_kf)
            self.scrollToPlayhead()

    def _nextKeyframeCb(self, action):
        position = self.app.current.pipeline.getPosition()
        next_kf = self.timeline.getNextKeyframe(position)
        if next_kf:
            self._seeker.seek(next_kf)
            self.scrollToPlayhead()

    def playPause(self, unused_action):
        self.app.current.pipeline.togglePlayback()

    def setTimeline(self, bTimeline):
        self.bTimeline = bTimeline
        self.timeline.selection.connect("selection-changed", self._selectionChangedCb)
        self.timeline.setTimeline(bTimeline)

    def _selectionChangedCb(self, selection):
        """
        The selected clips on the timeline canvas have changed with the
        "selection-changed" signal.

        This is where you apply global UI changes, unlike individual
        track elements' "selected-changed" signal from the Selected class.
        """
        if selection:
            self.selection_actions.set_sensitive(True)
        else:
            self.selection_actions.set_sensitive(False)

    def _scrollEventCb(self, embed, event):
        # FIXME : see https://bugzilla.gnome.org/show_bug.cgi?id=697522
        deltas = event.get_scroll_deltas()
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            if deltas[2] < 0:
                Zoomable.zoomIn()
            elif deltas[2] > 0:
                Zoomable.zoomOut()
            self._scrollToPlayhead()
        elif event.state & Gdk.ModifierType.SHIFT_MASK:
            if deltas[2] > 0:
                self._scrollDown()
            elif deltas[2] < 0:
                self._scrollUp()
        else:
            if deltas[2] > 0:
                self._scrollRight()
            elif deltas[2] < 0:
                self._scrollLeft()
        self.scrolled += 1

    def testTimeline(self, timeline):
        timeline.set_easing_duration(600)

        Gst.init([])
        GES.init()

        self.project = GES.Project(uri=None, extractable_type=GES.Timeline)

        bTimeline = GES.Timeline()
        bTimeline.add_track(GES.Track.audio_raw_new())
        bTimeline.add_track(GES.Track.video_raw_new())

        timeline.setTimeline(bTimeline)

        layer = GES.Layer()
        bTimeline.add_layer(layer)

        self.bTimeline = bTimeline

        self.project.connect("asset-added", self._doAssetAddedCb, layer)
        self.project.create_asset("file://" + sys.argv[1], GES.UriClip)

if __name__ == "__main__":
    # Basic argument handling, no need for getopt here
    if len(sys.argv) < 2:
        print "Supply a uri as argument"
        sys.exit()

    print "Starting stupid demo, using uri as a new clip, with start = 2, duration = 25 and inpoint = 5."
    print "Use ipython if you want to interact with the timeline in a more interesting way"
    print "ipython ; %gui gtk3 ; %run timeline.py ; help yourself"

    window = Gtk.Window()
    widget = Timeline()
    window.add(widget)
    window.maximize()
    window.show_all()
    widget.run()
