
from __future__ import division

from chimera.core.chimeraobject import ChimeraObject
from chimera.core.lock import lock
from chimera.core.event import event
from chimera.core.exceptions import ChimeraException, printException
from chimera.core.constants import SYSTEM_CONFIG_DIRECTORY

from chimera.interfaces.focuser import InvalidFocusPositionException

from chimera.controllers.imageserver.imagerequest import ImageRequest
from chimera.controllers.imageserver.util         import getImageServer

from chimera.util.enum import Enum
from chimera.util.image import Image
from chimera.util.sextractor import SExtractor

import numpy as N
import yaml

plot = True
try:
    import pylab as P
except (ImportError, RuntimeError):
    plot = False

from math import sqrt, ceil
import time
import os
import logging


Target = Enum("CURRENT", "AUTO")


class StarNotFoundException (ChimeraException):
    pass

class FocusNotFoundException (ChimeraException):
    pass


class FocusFit (object):

    def __init__ (self):

        # input
        self.temperature = None
        self.position = None
        self.fwhm = None
        self.minmax = None

        # calculated
        self.A = 0
        self.B = 0
        self.C = 0

        self.fwhm_fit = None
        self.err = 1e20

    best_focus = property(lambda self: (-self.B / (2*self.A),
                                       (-self.B**2 + 4*self.A*self.C) / (4*self.A)))
 
    def plot (self, filename):
        
        global plot
        
        if plot:
            P.plot(self.position, self.fwhm, "ro", label="data")
            #P.errorbar(self.position, self.fwhm_fit, yerr=self.err, fmt="k:",
            #           ms=15, label="fit")
            
            P.plot([self.best_focus[0]], [self.best_focus[1]], "bD",
                   label="best focus from fit")
            
            #P.legend()
            if self.minmax:
                P.ylim(*self.minmax)
                
            P.title("Focus")
            P.xlabel("Focus position")
            P.ylabel("FWHM (pixel)")
            P.savefig(filename)

    def log (self, filename):
        
        log = open(filename, "w")

        print >> log, "#", time.strftime("%c")
        print >> log, "# A=%f B=%f C=%f" % tuple(self)
        print >> log, "# best focus position: %.3f (FWHM %.3f)" % self.best_focus
        if self.minmax:
            print >> log, "# minmax filtering: %s" % str(self.minmax)

        if self.temperature:
            print >> log, "# focuser temperature: %.3f" % self.temperature

        for position, fwhm in zip(self.position, self.fwhm):
            print >> log, position, fwhm

        log.close()

    def __iter__ (self):
        return (self.A, self.B, self.C).__iter__()

    def __cmp__ (self, other):
        if isinstance(other, FocusFit):
            return (self.err - other.err)
        else:
            return (self.err - other)

    def __hash__ (self):
        return hash((self.A, self.B, self.C, self.err))

    def __nonzero__ (self):
        return (self.position != None) and (self.fwhm != None)

    @staticmethod
    def fit (position, fwhm, temperature=None, minmax=None):

        if minmax and len(minmax) >= 2:
            idxs = (fwhm >= minmax[0]) & (fwhm <= minmax[1])
            position = position[idxs]
            fwhm = fwhm[idxs]

        A, B, C = N.polyfit(position, fwhm, 2)

        fwhm_fit = N.polyval([A,B,C], position)
        
        err = sqrt(sum((fwhm_fit - fwhm)**2) / len(position))

        fit = FocusFit()
        fit.position = position
        fit.fwhm = fwhm
        fit.temperature = temperature
        fit.minmax = minmax

        fit.A, fit.B, fit.C = A,B,C
        fit.err = err
        fit.fwhm_fit = fwhm_fit

        return fit
    
class Autofocus (ChimeraObject):
    
    """
    Auto focuser
    ============

    This instrument will try to characterizes the current system and
    fit a parabola to a curve made of a star FWHM versus focus
    positions.

    1. determine target telescope position.
       if CURRENT, will use current telescope position.
       
       if AUTO: get site details and ask catalog manager to give us a
       list of stars available to focus now.
    
    2) take exposure to find focus star.

       If no star was found and in AUTO target mode, try another
       one. otherwise, abort the process.

    3) set window and binning if necessary and start iteration:

       Get n points starting at min_pos and ending at max_pos focus positions,
       and for each position measure FWHM of a target star (currently the
       brighter star in the field).

       Fit a parabola to the FWHM points measured.

    4) Leave focuser at best focus point (parabola vertice)

    """

    __config__ = {"telescope"          : "/Telescope/0",
                  "camera"             : "/Camera/0",
                  "filterwheel"        : "/FilterWheel/0",
                  "focuser"            : "/Focuser/0",
                  "max_stars_to_try"   : 5}

    def __init__ (self):
        ChimeraObject.__init__ (self)

        self.imageRequest = ImageRequest()
        self.filter = None
        
        self.currentRun = None

        self.best_fit = None

        self._debugging = False
        self._debug_images = []
        self._debug_image = 0

        self._log_handler = None

    @event
    def stepComplete (self, position, star, frame):
        """Raised after every step in the focus sequence with
        information about the last step.
        """

    def getTel(self):
        return self.getManager().getProxy(self["telescope"])

    def getCam(self):
        return self.getManager().getProxy(self["camera"])

    def getFilter(self):
        return self.getManager().getProxy(self["filterwheel"])

    def getFocuser(self):
        return self.getManager().getProxy(self["focuser"])

    def _getID(self):
        return time.strftime("%Y%m%d-%H%M%S")

    def _openLogger(self):

        if self._log_handler:
            self._closeLogger()
            
        self._log_handler = logging.FileHandler(os.path.join(SYSTEM_CONFIG_DIRECTORY, self.currentRun, "autofocus.log"))
        self._log_handler.setFormatter(logging.Formatter(fmt="%(message)s"))
        self._log_handler.setLevel(logging.DEBUG)
        self.log.addHandler(self._log_handler)

    def _closeLogger(self):
        if self._log_handler:
            self.log.removeHandler(self._log_handler)
            self._log_handler.close()

    @lock
    def focus (self, target=Target.CURRENT,
               filter=None, exptime=None, binning=None, window=None,
               start=2000, end=6000, step=500, points=None,
               minmax=None, debug=False):

        self._debugging = debug

        self.currentRun = self._getID()

        if not os.path.exists(os.path.join(SYSTEM_CONFIG_DIRECTORY, self.currentRun)):
            os.mkdir(os.path.join(SYSTEM_CONFIG_DIRECTORY, self.currentRun))

        self._openLogger()

        if debug:
            debug_file = open(os.path.join(debug, "autofocus.debug"), "r")
            debug_data = yaml.load(debug_file.read())

            start = debug_data["start"]
            end   = debug_data["end"]
            points= debug_data["points"]
            
            debug_file.close()
        else:
            # save parameter to ease a debug run later
            debug_data = dict(id=self.currentRun, start=start, end=end, points=len(positions))
            try:
                debug_file = open(os.path.join(SYSTEM_CONFIG_DIRECTORY, self.currentRun, "autofocus.debug"), "w")
                debug_file.write(yaml.dump(debug_data))
                debug_file.close()
            except IOError:
                self.log.warning("Cannot save debug information. Debug will be a little harder later.")

        # if points given, use to calculate step size
        if points:
            step = int(ceil(end-start)/points)
            positions = N.arange(start, end, step)
        else:
            positions = N.arange(start, end+1, step)

        self.log.debug("="*40)
        self.log.debug("[%s] Starting autofocus run." % time.strftime("%c"))
        self.log.debug("="*40)        
        self.log.debug("Focus range: start=%d end=%d step=%d points=%d" % (start, end, step, len(positions)))
        
        # images for debug mode
        if debug:
            self._debug_images = [ "%s/image-%02d.fits" % (debug, i)
                                   for i in range(len(positions)+1)]

        self.imageRequest["exp_time"] = exptime or 10
        self.imageRequest["frames"] = 1
        self.imageRequest["shutter"] = "OPEN"
        
        if filter:
            self.filter = filter
        if binning:
            self.imageRequest["binning"] = binning
        if window:
            self.imageRequest["window"] = window
        
        # 1. Find star to focus

        if target == Target.AUTO:
            raise NotImplementedError()
            # FIXME: impelemnt this!
            target_position = self._findStarToFocus()

            self.log.debug("Trying to move to the focus star at %s." % target_position)

            tel = self.getTel()
            tel.slewToRaDec(target_position)
            
        else:
            self.log.debug("Will look for stars in the current telescope position")

        # 2. Find best star to focus on this field

        star_found = self._findBestStarToFocus(self._takeImageAndResolveStars())

        if not star_found:

            if not target == Target.AUTO:
                raise StarNotFoundException("Couldn't find a suitable star to focus on.")

            tries = 1

            while not star_found and tries <= self["max_stars_to_try"]:
                star_found = self._findBestStarToFocus(self._takeImageAndResolveStars())
                tries += 1
                
            if not star_found:
                raise StarNotFoundException("Couldn't find a suitable star to focus on. "
                                            "Giving up after %d tries." % tries)

        try:
            fit = self._fitFocus(positions, minmax)

            if not self.best_fit or fit < self.best_fit:
                self.best_fit = fit
                
            return (self.currentRun, fit.A, fit.B, fit.C, fit.best_focus)

        finally:
            # reset debug counter
            self._debug_image = 0

    def _fitFocus (self, positions, minmax=None):
        
        focuser = self.getFocuser()
        initial_position = focuser.getPosition()

        self.log.debug("Initial focus position: %04d" % initial_position)

        fwhm  = N.zeros(len(positions))

        for i, position in enumerate(positions):

            self.log.debug("Moving focuser to %d" % int(position))

            focuser.moveTo(position)

            frame = self._takeImage()
            stars = self._findStars(frame.filename())
            star = self._findBrighterStar(stars)

            self.log.debug("Adding star to curve. (X,Y)=(%d,%d) FWHM=%.3f FLUX=%.3f" % (star["XWIN_IMAGE"], star["YWIN_IMAGE"],
                                                                                        star["FWHM_IMAGE"], star["FLUX_BEST"]))

            fwhm[i] = star["FWHM_IMAGE"]

            self.stepComplete(position, star, frame)

        # fit a parabola to the points and save parameters
        try:
            if minmax:
                self.log.debug("Minmax filtering FWHM (%.3f,%.3f)" % minmax)
            fit = FocusFit.fit(positions, fwhm, minmax=minmax)
        except Exception, e:
            focuser.moveTo(initial_position)

            raise FocusNotFoundException("Error trying to fit a focus curve. "
                                         "Leaving focuser at %04d" % initial_position)
            

        fit.plot(os.path.join(SYSTEM_CONFIG_DIRECTORY, self.currentRun, "autofocus.plot.png"))
        fit.log(os.path.join(SYSTEM_CONFIG_DIRECTORY, self.currentRun, "autofocus.plot.dat"))

        # leave focuser at best position
        try:
            focuser.moveTo(fit.best_focus[0])
            self.log.debug("Best focus position: %.3f" % fit.best_focus[0])
        except InvalidFocusPositionException:
            self.log.debug("Coundt' find best focus position. Check logs.")

        return fit
    
    def _takeImageAndResolveStars (self):

        frame = self._takeImage()
        stars = self._findStars(frame.filename())

        return stars

    def _takeImage (self):

        if self._debugging:
            try:
                frame = self._debug_images[self._debug_image]
                self._debug_image += 1

                img = Image.fromFile(frame)
                srv = getImageServer(self.getManager())
                return srv.register(img)
            except IndexError:
                raise ChimeraException("Cannot find debug images")

        if not self["save_frames"]:
            self.imageRequest["filename"] = "focus-$DATE"
        else:
            self.imageRequest["filename"] = os.path.join(SYSTEM_CONFIG_DIRECTORY, self.currentRun, "focus-$DATE.fits")

        cam = self.getCam()
        
        if self.filter:
            filter = self.getFilter()
            filter.setFilter(self.filter)
            
        frame = cam.expose(self.imageRequest)

        if frame:
            return frame[0]
        else:
            raise Exception("Error taking image.")

    def _findStars (self, fits_file):

        sex = SExtractor ()

        sex.config['PIXEL_SCALE'] = 0.45
        sex.config['BACK_TYPE']   = "AUTO"

        sex.config['SATUR_LEVEL'] = 60000

        # improve speed with higher threshold
        sex.config['DETECT_THRESH'] = 3.0

        # no output, please
        sex.config['VERBOSE_TYPE'] = "QUIET"

        # our "star" dict entry will contain all this members
        sex.config['PARAMETERS_LIST'] = ["NUMBER",
                                         "XWIN_IMAGE", "YWIN_IMAGE",
                                         "FLUX_BEST", "FWHM_IMAGE",
                                         "FLAGS"]

        # ok, here we go!
        sex.run(fits_file)

        result = sex.catalog()

        sex.clean(config=True, catalog=True, check=True)

        return result

    def _findBestStarToFocus (self, catalog):

        # simple plan: brighter star
        # FIXME: avoid "border" stars
        return self._findBrighterStar(catalog)

    def _findBrighterStar (self, catalog):

        fluxes = [star for star in catalog if star["FLAGS"] == 0]

        if not fluxes: # empty catalog
            return False

        return max(fluxes, key=lambda star: star["FLUX_BEST"])
        
