
# pylint: disable=too-many-statements

import math
import glob
import os
import copy
from contextlib import contextmanager

import numpy as np
import torch
import torch.nn.functional as F
import torchvision
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrow
from PIL import Image

from .network.process import laplace_sampling
from .utils import open_annotations, get_task_error
from .visuals.pifpaf_show import KeypointPainter, image_canvas
from .network import Loco
from .network.process import factory_for_gt, preprocess_pifpaf


def social_interactions(idx, centers, angles, dds, stds=None, social_distance=False,
                        n_samples=100, threshold_prob=0.25, threshold_dist=2, radii=(0.3, 0.5)):
    """
    return flag of alert if social distancing is violated
    """

    # A) Check whether people are close together
    xx = centers[idx][0]
    zz = centers[idx][1]
    distances = [math.sqrt((xx - centers[i][0]) ** 2 + (zz - centers[i][1]) ** 2) for i, _ in enumerate(centers)]
    sorted_idxs = np.argsort(distances)
    indices = [idx_t for idx_t in sorted_idxs[1:] if distances[idx_t] <= threshold_dist]

    # B) Check whether people are looking inwards and whether there are no intrusions
    # Deterministic
    if n_samples < 2:
        for idx_t in indices:
            if check_f_formations(idx, idx_t, centers, angles,
                                  radii=radii,  # Binary value
                                  social_distance=social_distance):
                return True

    # Probabilistic
    else:
        # Samples distance
        dds = torch.tensor(dds).view(-1, 1)
        stds = torch.tensor(stds).view(-1, 1)
        # stds_te = get_task_error(dds)  # similar results to MonoLoco but lower true positive
        laplace_d = torch.cat((dds, stds), dim=1)
        samples_d = laplace_sampling(laplace_d, n_samples=n_samples)

        # Iterate over close people
        for idx_t in indices:
            f_forms = []
            for s_d in range(n_samples):
                new_centers = copy.deepcopy(centers)
                for el in (idx, idx_t):
                    delta_d = dds[el] - float(samples_d[s_d, el])
                    theta = math.atan2(new_centers[el][1], new_centers[el][0])
                    delta_x = delta_d * math.cos(theta)
                    delta_z = delta_d * math.sin(theta)
                    new_centers[el][0] += delta_x
                    new_centers[el][1] += delta_z
                f_forms.append(check_f_formations(idx, idx_t, new_centers, angles,
                                                  radii=radii,
                                                  social_distance=social_distance))
            if (sum(f_forms) / n_samples) >= threshold_prob:
                return True
    return False


def check_f_formations(idx, idx_t, centers, angles, radii, social_distance=False):
    """
    Check F-formations for people close together (this function do not expect far away people):
    1) Empty space of a certain radius (no other people or themselves inside)
    2) People looking inward
    """

    # Extract centers and angles
    other_centers = np.array([cent for l, cent in enumerate(centers) if l not in (idx, idx_t)])
    theta0 = angles[idx]
    theta1 = angles[idx_t]

    # Find the center of o-space as average of two candidates (based on their orientation)
    for radius in radii:
        x_0 = np.array([centers[idx][0], centers[idx][1]])
        x_1 = np.array([centers[idx_t][0], centers[idx_t][1]])

        mu_0 = np.array([centers[idx][0] + radius * math.cos(theta0), centers[idx][1] - radius * math.sin(theta0)])
        mu_1 = np.array([centers[idx_t][0] + radius * math.cos(theta1), centers[idx_t][1] - radius * math.sin(theta1)])
        o_c = (mu_0 + mu_1) / 2

        # 1) Verify they are looking inwards.
        # The distance between mus and the center should be less wrt the original position and the center
        d_new = np.linalg.norm(mu_0 - mu_1) / 2 if social_distance else np.linalg.norm(mu_0 - mu_1)
        d_0 = np.linalg.norm(x_0 - o_c)
        d_1 = np.linalg.norm(x_1 - o_c)

        # 2) Verify no intrusion for third parties
        if other_centers.size:
            other_distances = np.linalg.norm(other_centers - o_c.reshape(1, -1), axis=1)
        else:
            other_distances = 100 * np.ones((1, 1))  # Condition verified if no other people

        # Binary Classification
        # if np.min(other_distances) > radius:  # Ablation without orientation
        if d_new <= min(d_0, d_1) and np.min(other_distances) > radius:
            return True
    return False


def predict(args):

    cnt = 0
    args.device = torch.device('cpu')
    if torch.cuda.is_available():
        args.device = torch.device('cuda')

    # Load data and model
    monoloco = Loco(model=args.model, net='monoloco_pp',
                    device=args.device, n_dropout=args.n_dropout, p_dropout=args.dropout)

    images = []
    images += glob.glob(args.glob)  # from cli as a string or linux converts

    # Option 1: Run PifPaf extract poses and run MonoLoco in a single forward pass
    if args.json_dir is None:
        from .network import PifPaf, ImageList
        pifpaf = PifPaf(args)
        data = ImageList(args.images, scale=args.scale)
        data_loader = torch.utils.data.DataLoader(
            data, batch_size=1, shuffle=False,
            pin_memory=args.pin_memory, num_workers=args.loader_workers)

        for idx, (image_paths, image_tensors, processed_images_cpu) in enumerate(data_loader):
            images = image_tensors.permute(0, 2, 3, 1)

            processed_images = processed_images_cpu.to(args.device, non_blocking=True)
            fields_batch = pifpaf.fields(processed_images)

            # unbatch
            for image_path, image, processed_image_cpu, fields in zip(
                    image_paths, images, processed_images_cpu, fields_batch):

                if args.output_directory is None:
                    output_path = image_path
                else:
                    file_name = os.path.basename(image_path)
                    output_path = os.path.join(args.output_directory, file_name)
                im_size = (float(image.size()[1] / args.scale),
                           float(image.size()[0] / args.scale))

                print('image', idx, image_path, output_path)

                _, _, pifpaf_out = pifpaf.forward(image, processed_image_cpu, fields)

                kk, dic_gt = factory_for_gt(im_size, name=image_path, path_gt=args.path_gt)
                image_t = image  # Resized tensor

                # Run Monoloco
                boxes, keypoints = preprocess_pifpaf(pifpaf_out, im_size, enlarge_boxes=False)
                dic_out = monoloco.forward(keypoints, kk)
                dic_out = monoloco.post_process(dic_out, boxes, keypoints, kk, dic_gt, reorder=False)

                # Print
                show_social(args, image_t, output_path, pifpaf_out, dic_out)

                print('Image {}\n'.format(cnt) + '-' * 120)
                cnt += 1

    # Option 2: Load json file of poses from PifPaf and run monoloco
    else:
        for idx, im_path in enumerate(images):

            # Load image
            with open(im_path, 'rb') as f:
                image = Image.open(f).convert('RGB')
            if args.output_directory is None:
                output_path = im_path
            else:
                file_name = os.path.basename(im_path)
                output_path = os.path.join(args.output_directory, file_name)

            im_size = (float(image.size[0] / args.scale),
                       float(image.size[1] / args.scale))  # Width, Height (original)
            kk, dic_gt = factory_for_gt(im_size, name=im_path, path_gt=args.path_gt)
            image_t = torchvision.transforms.functional.to_tensor(image).permute(1, 2, 0)

            # Load json
            basename, ext = os.path.splitext(os.path.basename(im_path))

            extension = ext + '.pifpaf.json'
            path_json = os.path.join(args.json_dir, basename + extension)
            annotations = open_annotations(path_json)

            # Run Monoloco
            boxes, keypoints = preprocess_pifpaf(annotations, im_size, enlarge_boxes=False)
            dic_out = monoloco.forward(keypoints, kk)
            dic_out = monoloco.post_process(dic_out, boxes, keypoints, kk, dic_gt, reorder=False)
            if args.social_distance:
                show_social(args, image, output_path, annotations, dic_out)

            print('Image {}\n'.format(cnt) + '-' * 120)
            cnt += 1


def show_social(args, image_t, output_path, annotations, dic_out):
    """Output frontal image with poses or combined with bird eye view"""

    assert 'front' in args.output_types or 'bird' in args.output_types, "outputs allowed: front and/or bird"

    angles = dic_out['angles']
    dds = dic_out['dds_pred']
    stds = dic_out['stds_ale']
    xz_centers = [[xx[0], xx[2]] for xx in dic_out['xyz_pred']]

    if 'front' in args.output_types:

        # Resize back the tensor image to its original dimensions
        if not 0.99 < args.scale < 1.01:
            size = (round(image_t.shape[0] / args.scale), round(image_t.shape[1] / args.scale))  # height width
            image_t = image_t.permute(2, 0, 1).unsqueeze(0)  # batch x channels x height x width
            image_t = F.interpolate(image_t, size=size).squeeze().permute(1, 2, 0)

        # Prepare color for social distancing
        colors = ['r' if social_interactions(idx, xz_centers, angles, dds,
                                             stds=stds,
                                             threshold_prob=args.threshold_prob,
                                             threshold_dist=args.threshold_dist,
                                             radii=args.radii)
                  else 'deepskyblue'
                  for idx, _ in enumerate(dic_out['xyz_pred'])]

        # Draw keypoints and orientation
        keypoint_sets, scores = get_pifpaf_outputs(annotations)
        uv_centers = dic_out['uv_heads']
        sizes = [abs(dic_out['uv_heads'][idx][1] - uv_s[1]) / 1.5 for idx, uv_s in
                 enumerate(dic_out['uv_shoulders'])]
        keypoint_painter = KeypointPainter(show_box=False)

        with image_canvas(image_t,
                          output_path + '.front.png',
                          show=args.show,
                          fig_width=10,
                          dpi_factor=1.0) as ax:
            keypoint_painter.keypoints(ax, keypoint_sets, colors=colors)
            draw_orientation(ax, uv_centers, sizes, angles, colors, mode='front')

    if 'bird' in args.output_types:
        with bird_canvas(args, output_path) as ax1:
            draw_orientation(ax1, xz_centers, [], angles, colors, mode='bird')
            draw_uncertainty(ax1, xz_centers, stds)


def get_pifpaf_outputs(annotations):
    """Extract keypoints sets and scores from output dictionary"""
    if not annotations:
        return [], []
    keypoints_sets = np.array([dic['keypoints'] for dic in annotations]).reshape(-1, 17, 3)
    score_weights = np.ones((keypoints_sets.shape[0], 17))
    score_weights[:, 3] = 3.0
    # score_weights[:, 5:] = 0.1
    # score_weights[:, -2:] = 0.0  # ears are not annotated
    score_weights /= np.sum(score_weights[0, :])
    kps_scores = keypoints_sets[:, :, 2]
    ordered_kps_scores = np.sort(kps_scores, axis=1)[:, ::-1]
    scores = np.sum(score_weights * ordered_kps_scores, axis=1)
    return keypoints_sets, scores


@contextmanager
def bird_canvas(args, output_path):
    fig, ax = plt.subplots(1, 1)
    fig.set_tight_layout(True)
    output_path = output_path + '.bird.png'
    x_max = args.z_max / 1.5
    ax.plot([0, x_max], [0, args.z_max], 'k--')
    ax.plot([0, -x_max], [0, args.z_max], 'k--')
    ax.set_ylim(0, args.z_max + 1)
    yield ax
    fig.savefig(output_path)
    plt.close(fig)
    print('Bird-eye-view image saved')


def draw_orientation(ax, centers, sizes, angles, colors, mode):

    if mode == 'front':
        length = 5
        fill = False
        alpha = 0.6
        zorder_circle = 0.5
        zorder_arrow = 5
        linewidth = 1.5
        edgecolor = 'k'
        radiuses = [s / 1.2 for s in sizes]
    else:
        length = 1.3
        head_width = 0.3
        linewidth = 2
        radiuses = [0.2] * len(centers)
        # length = 1.6
        # head_width = 0.4
        # linewidth = 2.7
        radiuses = [0.2] * len(centers)
        fill = True
        alpha = 1
        zorder_circle = 2
        zorder_arrow = 1

    for idx, theta in enumerate(angles):
        color = colors[idx]
        radius = radiuses[idx]

        if mode == 'front':
            x_arr = centers[idx][0] + (length + radius) * math.cos(theta)
            z_arr = length + centers[idx][1] + (length + radius) * math.sin(theta)
            delta_x = math.cos(theta)
            delta_z = math.sin(theta)
            head_width = max(10, radiuses[idx] / 1.5)

        else:
            edgecolor = color
            x_arr = centers[idx][0]
            z_arr = centers[idx][1]
            delta_x = length * math.cos(theta)
            delta_z = - length * math.sin(theta)  # keep into account kitti convention

        circle = Circle(centers[idx], radius=radius, color=color, fill=fill, alpha=alpha, zorder=zorder_circle)
        arrow = FancyArrow(x_arr, z_arr, delta_x, delta_z, head_width=head_width, edgecolor=edgecolor,
                           facecolor=color, linewidth=linewidth, zorder=zorder_arrow)
        ax.add_patch(circle)
        ax.add_patch(arrow)


def draw_uncertainty(ax, centers, stds):
    for idx, std in enumerate(stds):
        std = stds[idx]
        theta = math.atan2(centers[idx][1], centers[idx][0])
        delta_x = std * math.cos(theta)
        delta_z = std * math.sin(theta)
        x = (centers[idx][0] - delta_x, centers[idx][0] + delta_x)
        z = (centers[idx][1] - delta_z, centers[idx][1] + delta_z)
        ax.plot(x, z, color='g', linewidth=2.5)
