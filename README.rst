chimera-autofocus plugin
========================

Automatically focus telescopes with chimera observatory control system
https://github.com/astroufsc/chimera.

Usage
-----

Install and configure ``chimera-autofocus`` plugin. It depends of SEXtractor.


Installation
------------

::

    pip install -U https://github.com/astroufsc/chimera-autofocus/archive/master.zip


Configuration Example
---------------------

::

  - type: Autofocus
    name: autofocus
    focuser: 192.168.56.1:7666/OptecTCFS/optec
    camera: 192.168.56.1:7666/ASCOMCamera/apogee_AltaU16M
    filterwheel: 192.168.56.1:7666/ASCOMFilterWheel/apogee_AFW509R
    start: 1
    end: 3000
    step: 200



Contact
-------

For more information, contact us on chimera's discussion list:
https://groups.google.com/forum/#!forum/chimera-discuss

Bug reports and patches are welcome and can be sent over our GitHub page:
https://github.com/astroufsc/chimera-autofocus/
