""" A real-plot of the optimization process"""

from bokeh.models import ColumnDataSource, Legend
from bokeh.plotting import curdoc, figure
from bokeh.server.server import Server
from tornado.ioloop import IOLoop

from openmdao.recorders.sqlite_reader import SqliteCaseReader

try:
    from openmdao.utils.gui_testing_utils import get_free_port
except:
    # If get_free_port is unavailable, the default port will be used
    def get_free_port():
        return 5000

def _realtime_opt_plot_setup_parser(parser):
    """
    Set up the realtime plot subparser for the 'openmdao realtime_opt_plot' command.

    Parameters
    ----------
    parser : argparse subparser
        The parser we're adding options to.
    """
    parser.add_argument(
        "case_recorder_filename",
        type=str,
        nargs="*",
        help="Name of openmdao case recorder filename. It should contain driver cases",
    )

def _realtime_opt_plot_cmd(options, user_args):
    """
    Run the realtime_opt_plot command.

    Parameters
    ----------
    options : argparse Namespace
        Command line options.
    user_args : list of str
        Args to be passed to the user script.
    """

    realtime_opt_plot(
        options.case_recorder_filename,
    )


class CaseTracker:
    def __init__(self):
        self._case_ids_read = []

    def get_new_cases(self):
        # need to read this each time since the constructor does all of the actual reading
        # TODO - add code SqliteCaseReader for reading real-time data
        cr = SqliteCaseReader("create_cr_files_out/driver_history.db")
        case_ids = cr.list_cases("driver", out_stream=None)
        new_case_ids = [
            case_id for case_id in case_ids if case_id not in set(self._case_ids_read)
        ]
        if new_case_ids:
            # just get the first one
            case_id = new_case_ids[0]
            driver_case = cr.get_case(case_id)
            objs = driver_case.get_objectives()
            # assume only one obj
            value = next(iter(objs.values()))
            value = float(value)
            new_data = {
                "counter": int(driver_case.counter),
                "obj": value,
            }
            self._case_ids_read.append(
                case_id
            )  # remember that this one has been plotted
            return new_data
        return None



class RealTimeOptPlot(object):
    def __init__(self, case_recorder_filename, doc):

        source = ColumnDataSource(dict(iteration=[], obj=[]))

        # p = figure(height=500, tools="xpan,xwheel_zoom,xbox_zoom,reset")
        p = figure(tools="xpan,xwheel_zoom,xbox_zoom,reset",
                   width_policy="max" , height_policy="max",
                   title="Real-time Optimization Progress Plot",
        )
        p.x_range.follow = "start"

        p.title.text_font_size = '25px'
        p.title.text_color = "black"
        p.title.text_font = "arial"
        p.title.align = "center"
        p.title.background_fill_color = "#cccccc"

        l1 = p.line(x="iteration", y="obj", line_width=3, color="navy", source=source)
        p.xaxis.axis_label = "Driver iterations"
        p.yaxis.axis_label = "Model variables"
        p.xaxis.minor_tick_line_color = None
        
        p.axis.axis_label_text_font_style = 'bold'
        p.axis.axis_label_text_font_size = '20pt'

        legend = Legend(items=[
                ("obj" , [l1]),
            ], location="center")

        p.add_layout(legend, 'right')

        
        p.legend.title = 'Model Variables'
        p.legend.title_text_font_style = "bold"
        p.legend.title_text_font_size = "20px"

        p.xgrid.band_hatch_pattern = "/"
        p.xgrid.band_hatch_alpha = 0.6
        p.xgrid.band_hatch_color = "lightgrey"
        p.xgrid.band_hatch_weight = 0.5
        p.xgrid.band_hatch_scale = 10


        case_tracker = CaseTracker()

        def update():
            new_data = case_tracker.get_new_cases()
            if new_data:
                obj = new_data["obj"]
                counter = new_data["counter"]
                source.stream({"iteration": [counter], "obj": [obj]})

        doc.add_root(p)
        doc.add_periodic_callback(update, 50)
        doc.title = "OpenMDAO Optimization"

    
def realtime_opt_plot(case_recorder_filename):
    """
    Visualize a ??.

    Parameters
    ----------
    case_recorder_filename : MetaModelStructuredComp or MetaModelUnStructuredComp
        The metamodel component.
    """
    from bokeh.application.application import Application
    from bokeh.application.handlers import FunctionHandler

    def _make_realtime_opt_plot_doc(doc):
        RealTimeOptPlot(case_recorder_filename, doc=doc)

    port_number = get_free_port()

    try:
        server = Server({'/': Application(FunctionHandler(_make_realtime_opt_plot_doc))}, port=port_number)
        server.start()
        server.io_loop.add_callback(server.show, "/")
        
        print(f"Bokeh server running on http://localhost:{port_number}")
        server.io_loop.start()
    except KeyboardInterrupt as e:
        print(f"Server stopped due to keyboard interrupt")
    except Exception as e:
        print(f"Error starting Bokeh server: {e}")
    finally:
        print("Stopping server")
        server.stop()
