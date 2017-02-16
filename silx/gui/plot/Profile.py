# coding: utf-8
# /*##########################################################################
#
# Copyright (c) 2004-2017 European Synchrotron Radiation Facility
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
# ###########################################################################*/
"""Utility functions, toolbars and actions  to create profile on images
and stacks of images"""


__authors__ = ["V.A. Sole", "T. Vincent", "P. Knobel", "H. Payno"]
__license__ = "MIT"
__date__ = "11/01/2017"


import numpy 

from silx.image.bilinear import BilinearImage

from .. import icons
from .. import qt
from .Colors import cursorColorForColormap
from .PlotActions import PlotAction
from .PlotToolButtons import ProfileToolButton


def _alignedFullProfile(data, origin, scale, position, roiWidth, axis):
    """Get a profile along one axis on a stack of images

    :param numpy.ndarray data: 3D volume (stack of 2D images)
        The first dimension is the image index.
    :param origin: Origin of image in plot (ox, oy)
    :param scale: Scale of image in plot (sx, sy)
    :param float position: Position of profile line in plot coords
                           on the axis orthogonal to the profile direction.
    :param int roiWidth: Width of the profile in image pixels.
    :param int axis: 0 for horizontal profile, 1 for vertical.
    :return: profile image + effective ROI area corners in plot coords
    """
    assert axis in (0, 1)
    assert len(data.shape) == 3

    # Convert from plot to image coords
    imgPos = int((position - origin[1 - axis]) / scale[1 - axis])

    if axis == 1:  # Vertical profile
        # Transpose image to always do a horizontal profile
        data = numpy.transpose(data, (0, 2, 1))

    nimages, height, width = data.shape

    roiWidth = min(height, roiWidth)  # Clip roi width to image size

    # Get [start, end[ coords of the roi in the data
    start = int(int(imgPos) + 0.5 - roiWidth / 2.)
    start = min(max(0, start), height - roiWidth)
    end = start + roiWidth

    if start < height and end > 0:
        profile = data[:, max(0, start):min(end, height), :].mean(
            axis=1, dtype=numpy.float32)
    else:
        profile = numpy.zeros((nimages, width), dtype=numpy.float32)

    # Compute effective ROI in plot coords
    profileBounds = numpy.array(
        (0, width, width, 0),
        dtype=numpy.float32) * scale[axis] + origin[axis]
    roiBounds = numpy.array(
        (start, start, end, end),
        dtype=numpy.float32) * scale[1 - axis] + origin[1 - axis]

    if axis == 0:  # Horizontal profile
        area = profileBounds, roiBounds
    else:  # vertical profile
        area = roiBounds, profileBounds

    return profile, area


def _alignedPartialProfile(data, rowRange, colRange, axis):
    """Mean of a rectangular region (ROI) of a stack of images
    along a given axis.

    Returned values and all parameters are in image coordinates.

    :param numpy.ndarray data: 3D volume (stack of 2D images)
        The first dimension is the image index.
    :param rowRange: [min, max[ of ROI rows (upper bound excluded).
    :type rowRange: 2-tuple of int (min, max) with min < max
    :param colRange: [min, max[ of ROI columns (upper bound excluded).
    :type colRange: 2-tuple of int (min, max) with min < max
    :param int axis: The axis along which to take the profile of the ROI.
                     0: Sum rows along columns.
                     1: Sum columns along rows.
    :return: Profile image along the ROI as the mean of the intersection
             of the ROI and the image.
    """
    assert axis in (0, 1)
    assert len(data.shape) == 3
    assert rowRange[0] < rowRange[1]
    assert colRange[0] < colRange[1]

    nimages, height, width = data.shape

    # Range aligned with the integration direction
    profileRange = colRange if axis == 0 else rowRange

    profileLength = abs(profileRange[1] - profileRange[0])

    # Subset of the image to use as intersection of ROI and image
    rowStart = min(max(0, rowRange[0]), height)
    rowEnd = min(max(0, rowRange[1]), height)
    colStart = min(max(0, colRange[0]), width)
    colEnd = min(max(0, colRange[1]), width)

    imgProfile = numpy.mean(data[:, rowStart:rowEnd, colStart:colEnd],
                            axis=axis+1, dtype=numpy.float32)

    # Profile including out of bound area
    profile = numpy.zeros((nimages, profileLength), dtype=numpy.float32)

    # Place imgProfile in full profile
    offset = - min(0, profileRange[0])
    profile[:, offset:offset + imgProfile.shape[1]] = imgProfile

    return profile


def createProfile(roiInfo, currentData, params, lineWidth):
    """Create the profile line for the the given image.

    :param roiInfo: information about the ROI: start point, end point and
        type ("X", "Y", "D")
    :param numpy.ndarray currentData: the 2D image or the 3D stack of images
        on which we compute the profile.
    :param dict params: parameters of the plot, such as origin, scale
        and colormap
    :param int lineWidth: width of the profile line
    :return: `profile, area, profileName, xLabel`, where:
        - profile is a 2D array of the profiles of the stack of images.
          For a single image, the profile is a curve, so this parameter
          has a shape *(1, len(curve))*
        - area is a tuple of two 1D arrays with 4 values each. They represent
          the effective ROI area corners in plot coords.
        - profileName is a string describing the ROI, meant to be used as
          title of the profile plot
        - xLabel is a string describing the meaning of the X axis on the
          profile plot ("rows", "columns", "distance")

    :rtype: tuple(ndarray, (ndarray, ndarray), str, str)
    """
    if currentData is None or params is None or\
        roiInfo is None or lineWidth is None:
        return

    # force 3D data (stack of images)
    if len(currentData.shape) == 2:
        currentData3D = currentData.reshape((1,) + currentData.shape)
    elif len(currentData.shape) == 3:
        currentData3D = currentData

    origin, scale = params['origin'], params['scale']

    roiWidth = max(1, lineWidth)
    roiStart, roiEnd, lineProjectionMode = roiInfo

    if lineProjectionMode == 'X':  # Horizontal profile on the whole image
        profile, area = _alignedFullProfile(currentData3D,
                                            origin, scale,
                                            roiStart[1], roiWidth,
                                            axis=0)

        yMin, yMax = min(area[1]), max(area[1]) - 1
        if roiWidth <= 1:
            profileName = 'Y = %g' % yMin
        else:
            profileName = 'Y = [%g, %g]' % (yMin, yMax)
        xLabel = 'Columns'

    elif lineProjectionMode == 'Y':  # Vertical profile on the whole image
        profile, area = _alignedFullProfile(currentData3D,
                                            origin, scale,
                                            roiStart[0], roiWidth,
                                            axis=1)

        xMin, xMax = min(area[0]), max(area[0]) - 1
        if roiWidth <= 1:
            profileName = 'X = %g' % xMin
        else:
            profileName = 'X = [%g, %g]' % (xMin, xMax)
        xLabel = 'Rows'

    else:  # Free line profile

        # Convert start and end points in image coords as (row, col)
        startPt = ((roiStart[1] - origin[1]) / scale[1],
                   (roiStart[0] - origin[0]) / scale[0])
        endPt = ((roiEnd[1] - origin[1]) / scale[1],
                 (roiEnd[0] - origin[0]) / scale[0])

        if (int(startPt[0]) == int(endPt[0]) or
                int(startPt[1]) == int(endPt[1])):
            # Profile is aligned with one of the axes

            # Convert to int
            startPt = int(startPt[0]), int(startPt[1])
            endPt = int(endPt[0]), int(endPt[1])

            # Ensure startPt <= endPt
            if startPt[0] > endPt[0] or startPt[1] > endPt[1]:
                startPt, endPt = endPt, startPt

            if startPt[0] == endPt[0]:  # Row aligned
                rowRange = (int(startPt[0] + 0.5 - 0.5 * roiWidth),
                            int(startPt[0] + 0.5 + 0.5 * roiWidth))
                colRange = startPt[1], endPt[1] + 1
                profile = _alignedPartialProfile(currentData3D,
                                                 rowRange, colRange,
                                                 axis=0)

            else:  # Column aligned
                rowRange = startPt[0], endPt[0] + 1
                colRange = (int(startPt[1] + 0.5 - 0.5 * roiWidth),
                            int(startPt[1] + 0.5 + 0.5 * roiWidth))
                profile = _alignedPartialProfile(currentData3D,
                                                 rowRange, colRange,
                                                 axis=1)

            # Convert ranges to plot coords to draw ROI area
            area = (
                numpy.array(
                    (colRange[0], colRange[1], colRange[1], colRange[0]),
                    dtype=numpy.float32) * scale[0] + origin[0],
                numpy.array(
                    (rowRange[0], rowRange[0], rowRange[1], rowRange[1]),
                    dtype=numpy.float32) * scale[1] + origin[1])

        else:  # General case: use bilinear interpolation

            # Ensure startPt <= endPt
            if (startPt[1] > endPt[1] or (
                    startPt[1] == endPt[1] and startPt[0] > endPt[0])):
                startPt, endPt = endPt, startPt

            profile = []
            for slice_idx in range(currentData3D.shape[0]):
                bilinear = BilinearImage(currentData3D[slice_idx, :, :])

                profile.append(bilinear.profile_line(
                                        (startPt[0] - 0.5, startPt[1] - 0.5),
                                        (endPt[0] - 0.5, endPt[1] - 0.5),
                                        roiWidth))
            profile = numpy.array(profile)


            # Extend ROI with half a pixel on each end, and
            # Convert back to plot coords (x, y)
            length = numpy.sqrt((endPt[0] - startPt[0]) ** 2 +
                                (endPt[1] - startPt[1]) ** 2)
            dRow = (endPt[0] - startPt[0]) / length
            dCol = (endPt[1] - startPt[1]) / length

            # Extend ROI with half a pixel on each end
            startPt = startPt[0] - 0.5 * dRow, startPt[1] - 0.5 * dCol
            endPt = endPt[0] + 0.5 * dRow, endPt[1] + 0.5 * dCol

            # Rotate deltas by 90 degrees to apply line width
            dRow, dCol = dCol, -dRow

            area = (
                numpy.array((startPt[1] - 0.5 * roiWidth * dCol,
                             startPt[1] + 0.5 * roiWidth * dCol,
                             endPt[1] + 0.5 * roiWidth * dCol,
                             endPt[1] - 0.5 * roiWidth * dCol),
                            dtype=numpy.float32) * scale[0] + origin[0],
                numpy.array((startPt[0] - 0.5 * roiWidth * dRow,
                             startPt[0] + 0.5 * roiWidth * dRow,
                             endPt[0] + 0.5 * roiWidth * dRow,
                             endPt[0] - 0.5 * roiWidth * dRow),
                            dtype=numpy.float32) * scale[1] + origin[1])

        y0, x0 = startPt
        y1, x1 = endPt
        if x1 == x0 or y1 == y0:
            profileName = 'From (%g, %g) to (%g, %g)' % (x0, y0, x1, y1)
        else:
            m = (y1 - y0) / (x1 - x0)
            b = y0 - m * x0
            profileName = 'y = %g * x %+g ; width=%d' % (m, b, roiWidth)
        xLabel = 'Distance'

    return profile, area, profileName, xLabel


# ProfileToolBar ##############################################################

class ProfileToolBar(qt.QToolBar):
    """QToolBar providing profile tools operating on a :class:`PlotWindow`.

    Attributes:

    - plot: Associated :class:`PlotWindow`.
    - profileWindow: Associated :class:`PlotWindow` displaying the profile.
    - actionGroup: :class:`QActionGroup` of available actions.

    To run the following sample code, a QApplication must be initialized.
    First, create a PlotWindow and add a :class:`ProfileToolBar`.

    >>> from silx.gui.plot import PlotWindow
    >>> from silx.gui.plot.Profile import ProfileToolBar
    >>> from silx.gui import qt

    >>> plot = PlotWindow()  # Create a PlotWindow
    >>> toolBar = ProfileToolBar(plot=plot)  # Create a profile toolbar
    >>> plot.addToolBar(toolBar)  # Add it to plot
    >>> plot.show()  # To display the PlotWindow with the profile toolbar

    :param plot: :class:`PlotWindow` instance on which to operate.
    :param profileWindow: :class:`ProfileScanWidget` instance where to
                          display the profile curve or None to create one.
    :param str title: See :class:`QToolBar`.
    :param parent: See :class:`QToolBar`.
    """
    # TODO Make it a QActionGroup instead of a QToolBar

    _POLYGON_LEGEND = '__ProfileToolBar_ROI_Polygon'

    def __init__(self, parent=None, plot=None, profileWindow=None,
                 title='Profile Selection'):
        super(ProfileToolBar, self).__init__(title, parent)
        assert plot is not None
        self.plot = plot

        self._overlayColor = None
        self._defaultOverlayColor = 'red'  # update when active image change

        self._roiInfo = None  # Store start and end points and type of ROI

        if profileWindow is None:
            # Import here to avoid cyclic import
            from .PlotWindow import Plot1D  # noqa
            self.profileWindow = Plot1D(self)
            self._ownProfileWindow = True
        else:
            self.profileWindow = profileWindow
            self._ownProfileWindow = False

        # Actions
        self.browseAction = qt.QAction(
                icons.getQIcon('normal'),
                'Browsing Mode', None)
        self.browseAction.setToolTip(
                'Enables zooming interaction mode')
        self.browseAction.setCheckable(True)
        self.browseAction.triggered[bool].connect(self._browseActionTriggered)

        self.hLineAction = qt.QAction(
                icons.getQIcon('shape-horizontal'),
                'Horizontal Profile Mode', None)
        self.hLineAction.setToolTip(
                'Enables horizontal profile selection mode')
        self.hLineAction.setCheckable(True)
        self.hLineAction.toggled[bool].connect(self._hLineActionToggled)

        self.vLineAction = qt.QAction(
                icons.getQIcon('shape-vertical'),
                'Vertical Profile Mode', None)
        self.vLineAction.setToolTip(
                'Enables vertical profile selection mode')
        self.vLineAction.setCheckable(True)
        self.vLineAction.toggled[bool].connect(self._vLineActionToggled)

        self.lineAction = qt.QAction(
                icons.getQIcon('shape-diagonal'),
                'Free Line Profile Mode', None)
        self.lineAction.setToolTip(
                'Enables line profile selection mode')
        self.lineAction.setCheckable(True)
        self.lineAction.toggled[bool].connect(self._lineActionToggled)

        self.clearAction = qt.QAction(
                icons.getQIcon('image'),
                'Clear Profile', None)
        self.clearAction.setToolTip(
                'Clear the profile Region of interest')
        self.clearAction.setCheckable(False)
        self.clearAction.triggered.connect(self.clearProfile)

        # ActionGroup
        self.actionGroup = qt.QActionGroup(self)
        self.actionGroup.addAction(self.browseAction)
        self.actionGroup.addAction(self.hLineAction)
        self.actionGroup.addAction(self.vLineAction)
        self.actionGroup.addAction(self.lineAction)

        self.browseAction.setChecked(True)

        # Add actions to ToolBar
        self.addAction(self.browseAction)
        self.addAction(self.hLineAction)
        self.addAction(self.vLineAction)
        self.addAction(self.lineAction)
        self.addAction(self.clearAction)

        # Add width spin box to toolbar
        self.addWidget(qt.QLabel('W:'))
        self.lineWidthSpinBox = qt.QSpinBox(self)
        self.lineWidthSpinBox.setRange(0, 1000)
        self.lineWidthSpinBox.setValue(1)
        self.lineWidthSpinBox.valueChanged[int].connect(
                self._lineWidthSpinBoxValueChangedSlot)
        self.addWidget(self.lineWidthSpinBox)

        self.plot.sigInteractiveModeChanged.connect(
                self._interactiveModeChanged)

        # Enable toolbar only if there is an active image
        self.setEnabled(self.plot.getActiveImage(just_legend=True) is not None)
        self.plot.sigActiveImageChanged.connect(
                self._activeImageChanged)

        # will manage the close event
        self.profileWindow.installEventFilter(self)

    def eventFilter(self, qobject, event):
        """Observe when the close event is emitted to clear the profile

        :param qobject: the object observe
        :param event: the event received by qobject
        """
        if hasattr(self, "plot"):
            if event.type() in (qt.QEvent.Close, qt.QEvent.Hide):
                self.clearProfile()

        return qt.QToolBar.eventFilter(self, qobject, event)

    def _activeImageChanged(self, previous, legend):
        """Handle active image change: toggle enabled toolbar, update curve"""
        self.setEnabled(legend is not None)
        if legend is not None:
            # Update default profile color
            activeImage = self.plot.getActiveImage()
            if activeImage is not None:
                self._defaultOverlayColor = cursorColorForColormap(
                        activeImage[4]['colormap']['name'])

            self.updateProfile()

    def _lineWidthSpinBoxValueChangedSlot(self, value):
        """Listen to ROI width widget to refresh ROI and profile"""
        self.updateProfile()

    def _interactiveModeChanged(self, source):
        """Handle plot interactive mode changed:

        If changed from elsewhere, disable drawing tool
        """
        if source is not self:
            self.browseAction.setChecked(True)

    def _hLineActionToggled(self, checked):
        """Handle horizontal line profile action toggle"""
        if checked:
            self.plot.setInteractiveMode('draw', shape='hline',
                                         color=None, source=self)
            self.plot.sigPlotSignal.connect(self._plotWindowSlot)
        else:
            self.plot.sigPlotSignal.disconnect(self._plotWindowSlot)

    def _vLineActionToggled(self, checked):
        """Handle vertical line profile action toggle"""
        if checked:
            self.plot.setInteractiveMode('draw', shape='vline',
                                         color=None, source=self)
            self.plot.sigPlotSignal.connect(self._plotWindowSlot)
        else:
            self.plot.sigPlotSignal.disconnect(self._plotWindowSlot)

    def _lineActionToggled(self, checked):
        """Handle line profile action toggle"""
        if checked:
            self.plot.setInteractiveMode('draw', shape='line',
                                         color=None, source=self)
            self.plot.sigPlotSignal.connect(self._plotWindowSlot)
        else:
            self.plot.sigPlotSignal.disconnect(self._plotWindowSlot)

    def _browseActionTriggered(self, checked):
        """Handle browse action mode triggered by user."""
        if checked:
            self.clearProfile()
            self.plot.setInteractiveMode('zoom', source=self)
            self.profileWindow.hide()

    def _plotWindowSlot(self, event):
        """Listen to Plot to handle drawing events to refresh ROI and profile.
        """
        if event['event'] not in ('drawingProgress', 'drawingFinished'):
            return

        checkedAction = self.actionGroup.checkedAction()
        if checkedAction == self.hLineAction:
            lineProjectionMode = 'X'
        elif checkedAction == self.vLineAction:
            lineProjectionMode = 'Y'
        elif checkedAction == self.lineAction:
            lineProjectionMode = 'D'
        else:
            return

        roiStart, roiEnd = event['points'][0], event['points'][1]

        self._roiInfo = roiStart, roiEnd, lineProjectionMode
        self.updateProfile()

    @property
    def overlayColor(self):
        """The color to use for the ROI.

        If set to None (the default), the overlay color is adapted to the
        active image colormap and changes if the active image colormap changes.
        """
        return self._overlayColor or self._defaultOverlayColor

    @overlayColor.setter
    def overlayColor(self, color):
        self._overlayColor = color
        self.updateProfile()

    def clearProfile(self):
        """Remove profile curve and profile area."""
        self._roiInfo = None
        self.updateProfile()

    def updateProfile(self):
        """Update the displayed profile and profile ROI.

        This uses the current active image of the plot and the current ROI.
        """
        imageData = self.plot.getActiveImage()
        if imageData is None:
            return

        # Clean previous profile area, and previous curve
        self.plot.remove(self._POLYGON_LEGEND, kind='item')
        self.profileWindow.clear()
        self.profileWindow.setGraphTitle('')
        self.profileWindow.setGraphXLabel('X')
        self.profileWindow.setGraphYLabel('Y')

        self._createProfile(currentData=imageData[0], params=imageData[4])

    def _createProfile(self, currentData, params):
        """Create the profile line for the the given image.

        :param numpy.ndarray currentData: the image or the stack of images
            on which we compute the profile
        :param params: parameters of the plot, such as origin, scale
            and colormap
        """
        assert ('colormap' in params and 'z' in params)
        if self._roiInfo is None:
            return

        profile, area, profileName, xLabel = createProfile(
                roiInfo=self._roiInfo,
                currentData=currentData,
                params=params,
                lineWidth=self.lineWidthSpinBox.value())
        colorMap = params['colormap']

        self.profileWindow.setGraphTitle(profileName)

        dataIs3D = len(currentData.shape) > 2
        if dataIs3D:
            self.profileWindow.addImage(profile,
                                        legend=profileName,
                                        xlabel=xLabel,
                                        ylabel="Frame index (depth)",
                                        colormap=colorMap)
        else:
            coords = numpy.arange(len(profile[0]), dtype=numpy.float32)
            self.profileWindow.addCurve(coords, profile[0],
                                        legend=profileName,
                                        xlabel=xLabel,
                                        color=self.overlayColor)

        self.plot.addItem(area[0], area[1],
                          legend=self._POLYGON_LEGEND,
                          color=self.overlayColor,
                          shape='polygon', fill=True,
                          replace=False, z=params['z'] + 1)

        self._showProfileWindow()

    def _showProfileWindow(self):
        """If profile window was created in this widget,
        it tries to avoid overlapping this widget when shown"""
        if self._ownProfileWindow and not self.profileWindow.isVisible():
            winGeom = self.window().frameGeometry()
            qapp = qt.QApplication.instance()
            screenGeom = qapp.desktop().availableGeometry(self)

            spaceOnLeftSide = winGeom.left()
            spaceOnRightSide = screenGeom.width() - winGeom.right()

            profileWindowWidth = self.profileWindow.frameGeometry().width()
            if (profileWindowWidth < spaceOnRightSide or
                        spaceOnRightSide > spaceOnLeftSide):
                # Place profile on the right
                self.profileWindow.move(winGeom.right(), winGeom.top())
            else:
                # Not enough place on the right, place profile on the left
                self.profileWindow.move(
                        max(0, winGeom.left() - profileWindowWidth), winGeom.top())

        self.profileWindow.show()

    def hideProfileWindow(self):
        """Hide profile window.
        """
        self.profileWindow.hide()


class Profile3DAction(PlotAction):
    """PlotAction that emits a signal when checked, to notify

    :param plot: :class:`.PlotWidget` instance on which to operate.
    :param parent: See :class:`QAction`.
    """
    sigProfileDimensionChanged = qt.Signal(int)

    def __init__(self, plot, parent=None):
        # Uses two images for checked/unchecked states
        self._states = {
            1: (icons.getQIcon('profile1D'),
                    "Compute 1D profile"),
            2: (icons.getQIcon('profile2D'),
                   "Compute 2D profile")
        }

        icon, tooltip = self._states[True]
        super(Profile3DAction, self).__init__(
                plot=plot,
                icon=icon,
                text='Profile',
                tooltip=tooltip,
                triggered=self.__compute3DProfile,
                checkable=False,
                parent=parent)

    def __compute3DProfile(self, profileDimension):
        """Callback when the QAction is activated
        """
        icon, tooltip = self._states[profileDimension]
        self.setIcon(icon)
        self.setToolTip(tooltip)
        self.sigProfileDimensionChanged.emit(profileDimension)


class Profile3DToolBar(ProfileToolBar):
    def __init__(self, parent=None, plot=None, title='Profile Selection'):
        """QToolBar providing profile tools for an image or a stack of images.

        :param parent: the parent QWidget
        :param plot: :class:`PlotWindow` instance on which to operate.
        :param str title: See :class:`QToolBar`.
        :param parent: See :class:`QToolBar`.
        """
        from .PlotWindow import Plot1D, Plot2D      # noqa
        super(Profile3DToolBar, self).__init__(parent=parent, plot=plot,
                                               title=title)
        # create the main widget
        self.ndProfileWindow = qt.QWidget()
        self.ndProfileWindow.setWindowTitle('Profile window')
        self.ndProfileWindow.setLayout(qt.QVBoxLayout())
        self._profileWindow1D = Plot1D(parent=self.ndProfileWindow)
        self._profileWindow2D = Plot2D(parent=self.ndProfileWindow)
        self.ndProfileWindow.layout().addWidget(self._profileWindow1D)
        self.ndProfileWindow.layout().addWidget(self._profileWindow2D)
        # create the 3D toolbar
        self.__create3DProfileAction()

        # connect to remove the profile line (manage close event)
        self.ndProfileWindow.installEventFilter(self)

        # filter hide event when received (manage show and hide event)
        self._profileWindow1D.installEventFilter(self)
        self._profileWindow2D.installEventFilter(self)

    def __create3DProfileAction(self):
        """Initialize the Profile3DAction action
        """

        self.profile3dAction = ProfileToolButton(
            parent=self, plot=self.plot)

        # initial profile dimension is 3D
        self.profile3dAction.computeProfileIn2D()
        self._profileDimension = 2
        self._profileWindow1D.hide()
        self._profileWindow2D.hide()

        self.profile3dAction.setVisible(True)
        self.addWidget(self.profile3dAction)

        self.profile3dAction.sigDimensionChanged.connect(self._setProfileDimension)
        self._setProfileDimension(self._profileDimension)

    def _browseActionTriggered(self, checked):
        """Handle browse action mode triggered by user.
        This is overloaded from :class:`ProfileToolBar` to hide
        :attr:`ndProfileWindow` instead of :attr:`profileWindow`."""
        if checked:
            self.clearProfile()
            self.plot.setInteractiveMode('zoom', source=self)
            self.ndProfileWindow.hide()

    def eventFilter(self, qobject, event):
        """Observe the show and hide events of the widgets related to
        the profile plot (a container widget, a Plot1D and a Plot2D)

        :param qobject: the observed object
        :param event: the event received by qobject
        """
        if not hasattr(self, "plot"):
            return False  # allow further processing of event by following filters
        if event.type() in (qt.QEvent.Close, qt.QEvent.Hide):
            # when the container widget is closed/hidden, clear the profile
            if qobject is self.ndProfileWindow:
                self.clearProfile()

            # else if both the plot windows are closed/hidden,
            # make sure the container widget is hidden as well
            elif (qobject is self._profileWindow1D and self._profileWindow2D.isHidden() or
                  qobject is self._profileWindow2D and self._profileWindow1D.isHidden()):
                    self.ndProfileWindow.hide()


        return qt.QToolBar.eventFilter(self, qobject, event)

    def setChildVisibility(self):
        if self._profileDimension is 1:
            self._profileWindow1D.setVisible(True)
            self._profileWindow2D.setVisible(False)
        elif self._profileDimension is 2:
            self._profileWindow1D.setVisible(False)
            self._profileWindow2D.setVisible(True)

    def _setProfileDimension(self, dimension):
        """Set the dimension in which we want to compute the profile.
        Valid values are 1 and 2 for now

        :param int dimension: dimension of the profile
        """
        self._setActiveProfileWindow(dimension)
        profileIsVisible = self.ndProfileWindow.isVisible()
        if dimension is 2:
            if profileIsVisible:
                self._profileWindow1D.hide()
                self._profileWindow2D.show()
        elif dimension is 1:
            if profileIsVisible:
                self._profileWindow2D.hide()
                self._profileWindow1D.show()
        self.updateProfile()

    def _setActiveProfileWindow(self, dimension):
        """Set the active profile window depending on the dimension of
        the profile

        :param int dimension: dimension of the profile"""
        self._profileDimension = dimension
        if dimension is 2:
            self.profileWindow = self._profileWindow2D
        elif dimension is 1:
            self.profileWindow = self._profileWindow1D
        else:
            self.profileWindow = None

    def updateProfile(self):
        """Method overloaded from :class:`ProfileToolBar`,
        to pass the stack of images instead of just the active image.

        In 1D profile mode, use the regular parent method.
        """
        if self._profileDimension is 1:
            super(Profile3DToolBar, self).updateProfile()
        elif self._profileDimension is 2:
            stackData = self.plot.getStack(copy=False,
                                           returnNumpyArray=True)
            if stackData is None:
                return
            self.plot.remove(self._POLYGON_LEGEND, kind='item')
            self.profileWindow.clear()
            self.profileWindow.setGraphTitle('')
            self.profileWindow.setGraphXLabel('X')
            self.profileWindow.setGraphYLabel('Y')

            self._createProfile(currentData=stackData[0],
                                params=stackData[1])
        else:
            raise ValueError("Can't compute profile for data in %s" %
                             str(self._profileDimension))

    def _showProfileWindow(self):
        """If profile window was created in this widget,
        it tries to avoid overlapping this widget when shown
        In Profile3DToolBar we have a widget grouping profile windows for 1D and 2D.
        So we also have to manage this one
        """
        self.setChildVisibility()
        self.ndProfileWindow.show()
        super(Profile3DToolBar, self)._showProfileWindow()

    def hideProfileWindow(self):
        """Hide container window for profile windows.
        """
        self.ndProfileWindow.hide()

    def getProfileWindow1D(self):
        """Plot window used to display 1D profile curve.

        :return: :class:`Plot1D`
        """
        return self._profileWindow1D

    def getProfileWindow2D(self):
        """Plot window used to display 2D profile image.

        :return: :class:`Plot2D`
        """
        return self._profileWindow2D
